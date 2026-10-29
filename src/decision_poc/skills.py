"""Nexent-facing Skills over the urban event deduction chain (P6, item 1).

Why this file exists
--------------------
An MCP tool is one function call. A Skill is one *step of reasoning* that may
use several tools, decide the step failed, and say what that failure means for
the steps after it. Nexent orchestrates Skills, so the five steps of the demo
chain have to exist as things that can be listed, invoked and asserted on:

    事件理解 → 影响分析 → 资源检查 → 跨部门推演 → 证据审计

Design decisions worth defending in review
------------------------------------------
1. **Skills are adapters, not a second implementation.** Each one reads the
   semantic store / deduction engine built in P4-P6 and adds only the part an
   orchestrator needs: a JSON-Schema'd request, a stable payload, and a
   *degradation decision*. No business rule lives here; if a rule changed here
   the MCP path and the Skill path would drift.
2. **One envelope end to end.** Every Skill returns the same `ToolResult` the
   MCP tools return (`status` / `data` / `error` / `stats` / `context`). Nexent
   gets one shape to branch on, and `trace_id` survives the whole chain.
3. **`insufficient` is a result, not an exception.** A hop with no rule coverage
   returns `partial` with a machine-readable reason and *does not fabricate a
   value*. Only "the input is unusable" (unknown event, denied evidence) is
   `error`.
4. **The audit step is a gate.** If any claim fails the citation check, the
   audit returns `error` and `PipelineReport.publishable` stays false. A chain
   whose citations broke must not be presentable, otherwise "引用完整率 100%"
   becomes decoration.
5. **Nexent keeps prompts and tool bindings only.** `skill_manifest()` emits the
   binding table (name, stage, request schema, MCP tools, failure mode) from the
   same objects used at runtime, so the external config cannot silently drift
   from the code.

No model is called anywhere in this file.
"""

from __future__ import annotations

import json
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import Field, ValidationError, field_validator

from .contracts import StrictModel, ToolContext, ToolError, ToolResult, ToolStats
from .deduction import (
    DEDUCTION_STAGES,
    DEFAULT_RULES_PATH,
    ConflictKind,
    DeductionEngine,
    DeductionResult,
    DepartmentConflict,
    ResourceGap,
    RuleSet,
    StageStatus,
    build_rule_registry,
    claims_from,
    decision_id_for,
    district_of,
    load_rules,
    parse_counts,
    record,
    verify_rules,
)
from .governance import ErrorCode, blocked_by_visibility, make_tool_error
from .provenance import ProvenanceLedger
from .semantic import ObjectKind, SemanticQuery, TwinObject, TwinSemanticStore

SKILL_MANIFEST_VERSION = "1.1"

# 编排顺序即验收口径：先弄清事件，再谈影响，最后才允许出结论。
SKILL_ORDER: tuple[str, ...] = ("understand", "impact", "resource", "deduce", "audit")

# 影响范围里"被影响"的对象类型。片区/部门/资源/预案/气象不是受影响对象，
# 它们分别属于范围、责任方、处置手段和诱因，混进同一个列表会让"影响范围"
# 变成"图谱邻居清单"。
IMPACT_KINDS: tuple[ObjectKind, ...] = (
    ObjectKind.ROAD,
    ObjectKind.DRAINAGE,
    ObjectKind.WATER,
    ObjectKind.GAS,
    ObjectKind.POWER,
    ObjectKind.TELECOM,
    ObjectKind.PUMP_STATION,
    ObjectKind.WORK_SITE,
    ObjectKind.INCIDENT,
)


# --- requests -----------------------------------------------------------


class SkillRequest(StrictModel):
    """Every skill starts from the same three facts. Narrowing it per skill would
    force the orchestrator to reshape the same context five times."""

    event_id: str = Field(min_length=1)
    as_of: datetime
    caller_labels: list[str] = Field(default_factory=list)

    @field_validator("as_of", mode="before")
    @classmethod
    def parse_iso(cls, value: Any) -> Any:
        if isinstance(value, str):
            return datetime.fromisoformat(value)
        return value

    @field_validator("as_of")
    @classmethod
    def aware_as_of(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        return value


class ImpactRequest(SkillRequest):
    max_hops: int = Field(default=3, ge=1, le=4)
    min_confidence: float = Field(default=0.6, ge=0.0, le=1.0)


class AuditRequest(SkillRequest):
    persist: bool = True


# --- payloads -----------------------------------------------------------


class EventFacts(StrictModel):
    event_id: str
    incident_twin_id: str
    event_type: str = ""
    district: str | None = None
    road_id: str | None = None
    depth_cm: float | None = None
    response_level: str | None = None
    warning_level: str | None = None
    lifecycle_state: str = ""
    observed_at: datetime
    rule_coverage: dict[str, int] = Field(default_factory=dict)
    expired_evidence: int = 0
    data_gaps: list[str] = Field(default_factory=list)


class ImpactItem(StrictModel):
    kind: str
    name: str
    twin_id: str
    via_predicate: str = ""


class ImpactScope(StrictModel):
    affected: list[ImpactItem] = Field(default_factory=list)
    counts_by_kind: dict[str, int] = Field(default_factory=dict)
    roads: list[str] = Field(default_factory=list)
    districts: list[str] = Field(default_factory=list)
    departments: list[str] = Field(default_factory=list)
    triple_count: int = 0
    filtered_reason: str | None = None
    max_hops: int = 3


class ResourceLine(StrictModel):
    resource_id: str
    required: int = Field(ge=0)
    available: int = Field(ge=0)
    gap: int
    unit: str = "台"
    basis: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


class ResourceAssessment(StrictModel):
    lines: list[ResourceLine] = Field(default_factory=list)
    total_gap: int = 0
    # 需求/库存总量即便"够用"也要给出：只报缺口会让"够用"变成一句没有数字的话
    required_total: int = 0
    available_total: int = 0
    # 按片区归集后，同一个片区在不同推演里算出的数字一致，可直接比较或相加
    demand_by_district: dict[str, int] = Field(default_factory=dict)
    available_by_district: dict[str, int] = Field(default_factory=dict)
    enough: bool = True
    unavailable_reason: str | None = None


class StageView(StrictModel):
    stage: str
    status: str
    conclusion: dict[str, str] = Field(default_factory=dict)
    evidence_refs: list[str] = Field(default_factory=list)
    detail: str = ""


class DeductionView(StrictModel):
    stages: list[StageView] = Field(default_factory=list)
    conflicts: list[DepartmentConflict] = Field(default_factory=list)
    resource_gaps: list[ResourceGap] = Field(default_factory=list)
    action_order: list[str] = Field(default_factory=list)
    residual_risks: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    handoff_required: bool = False
    handoff_reasons: list[str] = Field(default_factory=list)
    exposed_warning_level: str = "none"
    exposed_hours: float = 0.0
    rules_version: str = ""
    snapshot_hash: str = ""


class ClaimAudit(StrictModel):
    claim_id: str
    kind: str
    supported: bool
    reasons: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    rule_id: str | None = None


class AuditReport(StrictModel):
    decision_id: str
    claims: list[ClaimAudit] = Field(default_factory=list)
    supported: int = 0
    rejected: int = 0
    citation_completeness: float = 1.0
    record_id: str | None = None
    record_hash: str | None = None
    chain_ok: bool = True
    assets: int = 0
    snapshot_hash: str = ""
    knowledge_version: str = ""


# --- runtime ------------------------------------------------------------


class SkillRuntime:
    """Shared, read-mostly state for one orchestration run.

    Built once per run on purpose: rebuilding the semantic store per skill would
    give five snapshots and make "which version did this conclusion come from"
    unanswerable. `deduce()` is memoised for the same reason -- three skills ask
    for the same deduction and must see the same object.
    """

    def __init__(
        self,
        dataset_root: str | Path | None = None,
        *,
        rules_path: str | Path = DEFAULT_RULES_PATH,
        knowledge_version: str = "kb-v1.0.0",
        ledger_dir: str | Path | None = None,
        ledger: ProvenanceLedger | None = None,
    ) -> None:
        from .urban_dataset import DEFAULT_DATASET_ROOT, load_urban_assets

        root = Path(dataset_root) if dataset_root else DEFAULT_DATASET_ROOT
        self.dataset_root = root
        self.rules_path = Path(rules_path)
        self.knowledge_version = knowledge_version
        self.rules: RuleSet = load_rules(self.rules_path)
        self.source_refs: dict[str, str] = dict(verify_rules(self.rules, root))
        self.store: TwinSemanticStore = _build_store(root, knowledge_version)
        self.engine = DeductionEngine(
            self.store, self.rules, source_refs=self.source_refs, dataset_root=root
        )

        rows = load_urban_assets(root)
        self.assets = {asset.asset_id: asset for asset, _ in rows}
        self.visibility = {
            asset.asset_id: record.visibility for asset, record in rows
        }
        self.objects: dict[str, TwinObject] = {obj.name: obj for obj in self.store.objects()}
        self.objects_by_id: dict[str, TwinObject] = {
            obj.twin_id: obj for obj in self.store.objects()
        }

        self._deductions: dict[tuple[str, str, tuple[str, ...]], DeductionResult] = {}
        self.ledger: ProvenanceLedger | None = ledger
        if self.ledger is None and ledger_dir is not None and Path(ledger_dir).is_dir():
            self.ledger = self._load_ledger(Path(ledger_dir))
        self.ledger_dir = Path(ledger_dir) if ledger_dir else None

    # --- construction helpers -------------------------------------------
    def _rule_registry(self) -> dict[str, Any]:
        return build_rule_registry(self.rules, self.source_refs)

    def _load_ledger(self, directory: Path) -> ProvenanceLedger:
        return ProvenanceLedger.load(
            directory,
            asset_fingerprints={
                asset_id: asset.checksum_sha256 for asset_id, asset in self.assets.items()
            },
            knowledge_version=self.knowledge_version,
            rules=self._rule_registry(),
        )

    def ensure_ledger(self) -> ProvenanceLedger:
        if self.ledger is not None:
            return self.ledger
        if self.ledger_dir is not None and self.ledger_dir.is_dir():
            self.ledger = self._load_ledger(self.ledger_dir)
            return self.ledger
        self.ledger = ProvenanceLedger(
            asset_fingerprints={
                asset_id: asset.checksum_sha256 for asset_id, asset in self.assets.items()
            },
            knowledge_version=self.knowledge_version,
            rules=self._rule_registry(),
        )
        return self.ledger

    # --- lookups ---------------------------------------------------------
    def object_for(self, name: str) -> TwinObject | None:
        return self.objects.get(name)

    def twin_id_of(self, name: str) -> str | None:
        obj = self.objects.get(name)
        return obj.twin_id if obj else None

    def district_of(self, obj: TwinObject) -> str | None:
        return district_of(self.objects, obj)

    def deduce(
        self, event_id: str, as_of: datetime, caller_labels: Sequence[str] = ()
    ) -> DeductionResult:
        key = (event_id, as_of.isoformat(), tuple(sorted(caller_labels)))
        if key not in self._deductions:
            self._deductions[key] = self.engine.deduce(
                event_id, as_of=as_of, caller_labels=list(caller_labels)
            )
        return self._deductions[key]

    def blocked_assets(self, refs: Sequence[str], caller_labels: Sequence[str]) -> list[str]:
        return blocked_by_visibility(refs, caller_labels, self.visibility)

    def required_labels(self, asset_ids: Sequence[str]) -> list[str]:
        labels: set[str] = set()
        for asset_id in asset_ids:
            labels.update(self.visibility.get(asset_id, ()) or ())
        return sorted(labels)


def _build_store(root: Path, knowledge_version: str) -> TwinSemanticStore:
    from .semantic import build_urban_semantic_store

    return build_urban_semantic_store(str(root), knowledge_version=knowledge_version)


# --- envelope helpers ---------------------------------------------------


def _ok(skill: str, stage: str, payload: StrictModel, version: str, degraded: Sequence[str] = ()) -> ToolResult:
    return ToolResult(
        status="ok",
        data=_data(skill, stage, payload, degraded),
        error=None,
        stats=ToolStats(attempt=1, latency_ms=0),
        context=ToolContext(source=f"skill:{skill}", version=version),
    )


def _partial(
    skill: str,
    stage: str,
    payload: StrictModel,
    code: ErrorCode,
    message: str,
    version: str,
    degraded: Sequence[str] = (),
) -> ToolResult:
    # `partial` 不能带 error（信封规定），但调用方必须知道缺了什么：
    # 缺失原因放进 data["degraded"] 与 data["notice"]，而不是伪造一个 error。
    data = _data(skill, stage, payload, [*degraded, code.value])
    data["notice"] = message
    return ToolResult(
        status="partial",
        data=data,
        error=None,
        stats=ToolStats(attempt=1, latency_ms=0),
        context=ToolContext(source=f"skill:{skill}", version=version),
    )


def _error(skill: str, stage: str, code: ErrorCode, message: str, version: str, **extra: Any) -> ToolResult:
    data = {"skill": skill, "stage": stage, "payload": None, "degraded": [code.value], **extra}
    return ToolResult(
        status="error",
        data=data,
        error=make_tool_error(code, message),
        stats=ToolStats(attempt=1, latency_ms=0),
        context=ToolContext(source=f"skill:{skill}", version=version),
    )


def _data(skill: str, stage: str, payload: StrictModel, degraded: Sequence[str]) -> dict[str, Any]:
    return {
        "skill": skill,
        "stage": stage,
        "payload": payload.model_dump(mode="json"),
        "degraded": [item for item in degraded if item],
    }


class _Outcome(StrictModel):
    """Internal: what a skill decided, before it becomes an envelope."""

    status: str
    payload: StrictModel | None = None
    code: ErrorCode | None = None
    message: str = ""
    degraded: list[str] = Field(default_factory=list)
    extra: dict[str, Any] = Field(default_factory=dict)


def _fail(code: ErrorCode, message: str, **extra: Any) -> _Outcome:
    return _Outcome(status="error", code=code, message=message, extra=extra)


def _degrade(
    payload: StrictModel, code: ErrorCode, message: str, note: str
) -> _Outcome:
    return _Outcome(
        status="partial", payload=payload, code=code, message=message, degraded=[note]
    )


# --- skills -------------------------------------------------------------


class Skill:
    name: str = ""
    stage: str = ""
    description: str = ""
    request_model: type[SkillRequest] = SkillRequest
    tools: tuple[str, ...] = ()
    failure_mode: str = ""

    def _run(self, runtime: SkillRuntime, request: SkillRequest) -> _Outcome:
        raise NotImplementedError

    def invoke(self, runtime: SkillRuntime, request: Mapping[str, Any] | SkillRequest) -> ToolResult:
        started = time.perf_counter()
        if isinstance(request, self.request_model):
            parsed = request
        else:
            raw = request.model_dump() if isinstance(request, SkillRequest) else request
            try:
                parsed = self.request_model.model_validate(raw)
            except ValidationError as exc:
                return _error(
                    self.name,
                    self.stage,
                    ErrorCode.VALIDATION_FAILED,
                    f"参数校验失败: {exc.error_count()} 处",
                    runtime.knowledge_version,
                    invalid=json.loads(exc.json()),
                )
        try:
            outcome = self._run(runtime, parsed)
        except Exception as exc:  # 技能边界：任何未预期异常都要变成信封，不能让编排层裸崩
            return _error(
                self.name,
                self.stage,
                ErrorCode.INTERNAL_ERROR,
                f"{type(exc).__name__}: {exc}",
                runtime.knowledge_version,
            )
        elapsed = int((time.perf_counter() - started) * 1000)
        if outcome.status == "error":
            assert outcome.code is not None
            result = _error(
                self.name,
                self.stage,
                outcome.code,
                outcome.message,
                runtime.knowledge_version,
                **outcome.extra,
            )
        elif outcome.status == "partial":
            assert outcome.payload is not None and outcome.code is not None
            result = _partial(
                self.name,
                self.stage,
                outcome.payload,
                outcome.code,
                outcome.message,
                runtime.knowledge_version,
                outcome.degraded,
            )
        else:
            payload = outcome.payload or EventFacts(
                event_id=parsed.event_id,
                incident_twin_id="",
                observed_at=parsed.as_of,
            )
            result = _ok(
                self.name, self.stage, payload, runtime.knowledge_version, outcome.degraded
            )
        return result.model_copy(
            update={
                "stats": ToolStats(attempt=1, latency_ms=elapsed),
                "context": result.context.model_copy(
                    update={"version": runtime.knowledge_version}
                ),
            }
        )


class UnderstandSkill(Skill):
    name = "event_understand"
    stage = "understand"
    description = "把工单/事件编号解析成可推演的事件事实：位置、量级、响应等级、规则覆盖与数据缺口。"
    tools = ("semantic_query", "twin_neighbors", "evidence_locate")
    failure_mode = "事件不存在或不是事件对象 -> NOT_FOUND / VALIDATION_FAILED，整链终止"

    def _run(self, runtime: SkillRuntime, request: SkillRequest) -> _Outcome:
        obj = runtime.object_for(request.event_id)
        if obj is None:
            return _fail(ErrorCode.NOT_FOUND, f"未知事件: {request.event_id}")
        if obj.kind is not ObjectKind.INCIDENT:
            return _fail(
                ErrorCode.VALIDATION_FAILED,
                f"{request.event_id} 是 {obj.kind.value}，不能作为事件起点",
            )

        # 量级与响应等级必须与推演引擎同源：积水深度是从工单描述里解析出来的，
        # 在这里再写一遍正则就是第二套口径，两处迟早不一致。
        try:
            root = runtime.deduce(request.event_id, request.as_of, request.caller_labels).root
        except KeyError:
            return _fail(ErrorCode.NOT_FOUND, f"未知事件: {request.event_id}")

        gaps: list[str] = []
        if root.depth_cm is None:
            gaps.append("工单描述中未解析出积水深度，封路阈值类规则无法判定")
        if not obj.attributes.get("road_id"):
            gaps.append("缺少所在道路，影响范围只能按片区扩散")
        if root.district is None:
            gaps.append("缺少行政片区，跨区资源评估不可用")
        # 已知数据集缺口，写进输出而不是留给人去发现
        if "area_m2" not in obj.attributes:
            gaps.append("缺少积水面积，封路规则的“面积≥200m²”分支永不参与判定")

        # 同片区已过期、被时态层排除的气象依据：这是"旧预警当成现行预警"的直接证据
        expired = sum(
            1
            for triple in runtime.store.triples()
            if not triple.active_at(request.as_of)
            and any(
                runtime.objects_by_id.get(twin_id) is not None
                and runtime.objects_by_id[twin_id].kind is ObjectKind.WEATHER
                and runtime.district_of(runtime.objects_by_id[twin_id]) == root.district
                for twin_id in (triple.subject_id, triple.object_id)
            )
        )

        facts = EventFacts(
            event_id=request.event_id,
            incident_twin_id=obj.twin_id,
            event_type=obj.attributes.get("order_type", ""),
            district=root.district,
            road_id=obj.attributes.get("road_id"),
            depth_cm=root.depth_cm,
            response_level=root.response_level,
            warning_level=root.warning_level,
            lifecycle_state=obj.lifecycle_state.value,
            observed_at=obj.state_as_of,
            rule_coverage={
                stage: len(runtime.rules.by_stage(stage)) for stage in DEDUCTION_STAGES
            },
            expired_evidence=expired,
            data_gaps=gaps,
        )
        if root.depth_cm is None:
            return _degrade(
                facts,
                ErrorCode.EVIDENCE_INSUFFICIENT,
                "事件缺少量级字段，后续阈值判定将不可用",
                "no_depth",
            )
        return _Outcome(status="ok", payload=facts)


class ImpactSkill(Skill):
    name = "impact_analysis"
    stage = "impact"
    description = "沿真实传导关系扩散到受影响对象；片区只作为范围标签，不作为传导介质。"
    request_model = ImpactRequest
    tools = ("semantic_query", "twin_neighbors", "evidence_locate")
    failure_mode = "种子不可达 -> 空范围 + 原因码；越权证据被过滤 -> partial"

    def _run(self, runtime: SkillRuntime, request: SkillRequest) -> _Outcome:
        assert isinstance(request, ImpactRequest)
        obj = runtime.object_for(request.event_id)
        if obj is None:
            return _fail(ErrorCode.NOT_FOUND, f"未知事件: {request.event_id}")

        query = SemanticQuery.impact(
            query_id=f"impact:{request.event_id}:{request.as_of.isoformat()}",
            as_of=request.as_of,
            seed_ids=[obj.twin_id],
            max_hops=request.max_hops,
            min_confidence=request.min_confidence,
            caller_labels=request.caller_labels,
        )
        result = runtime.store.query(query)
        if result.status == "error":
            code = result.error.code if result.error else ErrorCode.INTERNAL_ERROR.value
            return _fail(
                _as_error_code(code),
                result.error.message if result.error else "语义查询失败",
            )

        scope = _scope_from(runtime, result.data, seed_twin=obj.twin_id, request=request)
        reason = result.data.get("reason")
        if reason:
            return _degrade(
                scope,
                ErrorCode.PERMISSION_DENIED
                if reason == "PERMISSION_FILTERED"
                else ErrorCode.EVIDENCE_INSUFFICIENT,
                f"影响范围不完整：{reason}",
                str(reason),
            )
        return _Outcome(status="ok", payload=scope)


class ResourceSkill(Skill):
    name = "resource_check"
    stage = "resource"
    description = "按预案折算系数算需求、比对库存、给出缺口；参数规则缺失时不给数字。"
    tools = ("event_deduce", "evidence_locate")
    failure_mode = "折算参数不可用 -> partial，缺口留空而不是填 0"

    def _run(self, runtime: SkillRuntime, request: SkillRequest) -> _Outcome:
        obj = runtime.object_for(request.event_id)
        if obj is None:
            return _fail(ErrorCode.NOT_FOUND, f"未知事件: {request.event_id}")

        outcome = runtime.deduce(request.event_id, request.as_of, request.caller_labels)
        stage_steps = outcome.stage("resource")
        if not stage_steps:
            return _fail(ErrorCode.EVIDENCE_INSUFFICIENT, "推演未产出资源跳")
        step = stage_steps[0]

        blocked = runtime.blocked_assets(step.evidence_refs, request.caller_labels)
        if blocked:
            return _fail(
                ErrorCode.PERMISSION_DENIED,
                "资源测算引用了当前调用方不可见的依据",
                blocked_assets=blocked,
                required_labels=runtime.required_labels(blocked),
            )

        if step.status is StageStatus.INSUFFICIENT:
            assessment = ResourceAssessment(
                lines=[],
                total_gap=0,
                enough=False,
                unavailable_reason=step.conclusion.get("error") or step.detail or "参数不足",
            )
            return _degrade(
                assessment,
                ErrorCode.EVIDENCE_INSUFFICIENT,
                "资源测算所需参数规则不可用，本跳不产出缺口结论",
                "resource_insufficient",
            )

        lines = [
            ResourceLine(
                resource_id=gap.resource_id,
                required=gap.required,
                available=gap.available,
                gap=gap.gap,
                unit=gap.unit,
                basis=list(gap.basis),
                evidence_refs=list(gap.evidence_refs),
            )
            for gap in outcome.resource_gaps
        ]
        assessment = ResourceAssessment(
            lines=lines,
            total_gap=sum(line.gap for line in lines),
            required_total=int(_num(step.conclusion.get("required")) or 0),
            available_total=int(_num(step.conclusion.get("available")) or 0),
            demand_by_district=parse_counts(step.conclusion.get("demand_by_district")),
            available_by_district=parse_counts(step.conclusion.get("available_by_district")),
            enough=all(line.gap == 0 for line in lines),
        )
        return _Outcome(status="ok", payload=assessment)


class DeduceSkill(Skill):
    name = "cross_department_deduction"
    stage = "deduce"
    description = "六跳推演：现象→对象→部门→冲突→资源→动作，含二次风险与转人工信号。"
    tools = ("event_deduce", "conflict_check", "evidence_locate")
    failure_mode = "结论引用了调用方不可见的依据 -> PERMISSION_DENIED（不降级删证据）"

    def _run(self, runtime: SkillRuntime, request: SkillRequest) -> _Outcome:
        obj = runtime.object_for(request.event_id)
        if obj is None:
            return _fail(ErrorCode.NOT_FOUND, f"未知事件: {request.event_id}")

        outcome = runtime.deduce(request.event_id, request.as_of, request.caller_labels)
        cited = _cited_refs(outcome)
        blocked = runtime.blocked_assets(cited, request.caller_labels)
        if blocked:
            return _fail(
                ErrorCode.PERMISSION_DENIED,
                "推演引用了对当前调用方不可见的依据，不能只给结论不给依据",
                blocked_assets=blocked,
                required_labels=runtime.required_labels(blocked),
            )

        view = DeductionView(
            stages=[
                StageView(
                    stage=step.stage,
                    status=step.status.value,
                    conclusion=dict(step.conclusion),
                    evidence_refs=list(step.evidence_refs),
                    detail=step.detail,
                )
                for step in outcome.steps
            ],
            conflicts=list(outcome.conflicts),
            resource_gaps=list(outcome.resource_gaps),
            action_order=outcome.action_order,
            residual_risks=list(outcome.residual_risks),
            assumptions=list(outcome.assumptions),
            handoff_required=outcome.handoff_required,
            handoff_reasons=list(outcome.handoff_reasons),
            exposed_warning_level=outcome.exposed_warning_level,
            exposed_hours=outcome.exposed_hours,
            rules_version=outcome.rules_version,
            snapshot_hash=outcome.snapshot_hash,
        )
        insufficient = [
            step.stage for step in outcome.steps if step.status is StageStatus.INSUFFICIENT
        ]
        if insufficient:
            return _degrade(
                view,
                ErrorCode.EVIDENCE_INSUFFICIENT,
                f"推演存在证据不足的跳: {sorted(set(insufficient))}",
                "insufficient_stages",
            )
        return _Outcome(status="ok", payload=view)


class AuditSkill(Skill):
    name = "evidence_audit"
    stage = "audit"
    description = "把结论转成 claim 过引用闸门、落哈希链账本，并返回可逐条追溯的审计报告。"
    request_model = AuditRequest
    tools = ("claim_trace", "provenance_verify", "evidence_locate")
    failure_mode = "任一 claim 不达标 -> EVIDENCE_INSUFFICIENT，不落账、整链不可发布"

    def _run(self, runtime: SkillRuntime, request: SkillRequest) -> _Outcome:
        assert isinstance(request, AuditRequest)
        obj = runtime.object_for(request.event_id)
        if obj is None:
            return _fail(ErrorCode.NOT_FOUND, f"未知事件: {request.event_id}")

        outcome = runtime.deduce(request.event_id, request.as_of, request.caller_labels)
        claims = claims_from(outcome)
        if not claims:
            return _fail(ErrorCode.EVIDENCE_INSUFFICIENT, "推演未产出可追溯的结论")

        # 审计会展示规则出处与资产定位，因此同样受可见性约束
        cited = sorted({ref for claim in claims for ref in claim.evidence_refs})
        blocked = runtime.blocked_assets(cited, request.caller_labels)
        if blocked:
            return _fail(
                ErrorCode.PERMISSION_DENIED,
                "审计会展示当前调用方不可见的依据",
                blocked_assets=blocked,
                required_labels=runtime.required_labels(blocked),
            )

        ledger = runtime.ensure_ledger()
        checks = {check.claim_id: check for check in ledger.register(claims)}
        audits = [
            ClaimAudit(
                claim_id=claim.claim_id,
                kind=claim.kind.value,
                supported=checks[claim.claim_id].supported,
                reasons=list(checks[claim.claim_id].reasons),
                evidence_refs=list(claim.evidence_refs),
                rule_id=claim.rule_id,
            )
            for claim in claims
        ]
        rejected = [audit for audit in audits if not audit.supported]
        report = AuditReport(
            decision_id=decision_id_for(outcome),
            claims=audits,
            supported=len(audits) - len(rejected),
            rejected=len(rejected),
            citation_completeness=ledger.citation_completeness(),
            snapshot_hash=outcome.snapshot_hash,
            knowledge_version=ledger.knowledge_version,
        )
        if rejected:
            return _fail(
                ErrorCode.EVIDENCE_INSUFFICIENT,
                f"{len(rejected)} 条结论未通过引用闸门，未落账",
                rejected=[item.claim_id for item in rejected],
                audit=report.model_dump(mode="json"),
            )

        if request.persist:
            rec = record(outcome, ledger)
            report = report.model_copy(
                update={
                    "record_id": rec.record_id,
                    "record_hash": rec.record_hash,
                    "assets": len(rec.asset_fingerprints),
                    "chain_ok": ledger.verify().ok,
                }
            )
            if runtime.ledger_dir is not None:
                ledger.save(runtime.ledger_dir)
        else:
            report = report.model_copy(update={"chain_ok": ledger.verify().ok})

        return _Outcome(status="ok", payload=report)


SKILLS: tuple[Skill, ...] = (
    UnderstandSkill(),
    ImpactSkill(),
    ResourceSkill(),
    DeduceSkill(),
    AuditSkill(),
)
SKILL_BY_NAME: dict[str, Skill] = {skill.name: skill for skill in SKILLS}
SKILL_BY_STAGE: dict[str, Skill] = {skill.stage: skill for skill in SKILLS}


# --- pipeline -----------------------------------------------------------


class SkillStep(StrictModel):
    skill: str
    stage: str
    status: str
    elapsed_ms: int = 0
    error_code: str | None = None
    degraded: list[str] = Field(default_factory=list)


class PipelineReport(StrictModel):
    event_id: str
    as_of: datetime
    caller_labels: list[str] = Field(default_factory=list)
    status: str = "ok"
    steps: list[SkillStep] = Field(default_factory=list)
    facts: EventFacts | None = None
    impact: ImpactScope | None = None
    resources: ResourceAssessment | None = None
    deduction: DeductionView | None = None
    audit: AuditReport | None = None
    handoff_required: bool = False
    handoff_reasons: list[str] = Field(default_factory=list)
    stop_reason: str | None = None
    publishable: bool = False
    snapshot_hash: str = ""
    knowledge_version: str = ""

    def step(self, stage: str) -> SkillStep | None:
        for item in self.steps:
            if item.stage == stage:
                return item
        return None


class SkillPipeline:
    """Runs the five skills in the fixed order, with explicit stop conditions.

    Degradation policy (the part an orchestrator cannot guess):

    | failure | effect |
    | --- | --- |
    | event not found / not an event | stop: nothing downstream is meaningful |
    | impact partial (permission / empty) | continue, mark degraded |
    | resource insufficient | continue, gap stays unknown (never 0) |
    | deduce denied / insufficient | continue to audit only if claims exist, else stop |
    | audit rejects a claim | stop, `publishable=False` |

    `publishable` is the single flag the Demo layer is allowed to trust.
    """

    def __init__(self, runtime: SkillRuntime | None = None) -> None:
        self.runtime = runtime or SkillRuntime()

    def run(
        self,
        event_id: str,
        as_of: datetime | str,
        *,
        caller_labels: Sequence[str] = (),
        persist: bool = True,
    ) -> PipelineReport:
        request = SkillRequest(
            event_id=event_id, as_of=as_of, caller_labels=list(caller_labels)
        )
        report = PipelineReport(
            event_id=request.event_id,
            as_of=request.as_of,
            caller_labels=list(request.caller_labels),
            knowledge_version=self.runtime.knowledge_version,
            snapshot_hash=self.runtime.store.snapshot_hash(),
        )
        collected: dict[str, ToolResult] = {}

        for skill in SKILLS:
            payload_request: SkillRequest = request
            if skill.name == "impact_analysis":
                payload_request = ImpactRequest(**request.model_dump())
            elif skill.name == "evidence_audit":
                payload_request = AuditRequest(persist=persist, **request.model_dump())

            result = skill.invoke(self.runtime, payload_request)
            collected[skill.stage] = result
            report.steps.append(
                SkillStep(
                    skill=skill.name,
                    stage=skill.stage,
                    status=result.status,
                    elapsed_ms=result.stats.latency_ms,
                    error_code=result.error.code if result.error else None,
                    degraded=list(result.data.get("degraded", [])),
                )
            )
            self._attach(report, skill.stage, result)

            if result.status == "error" and skill.stage in {"understand", "audit"}:
                report.status = "error" if skill.stage == "understand" else "partial"
                report.stop_reason = (
                    f"{skill.name}: {result.error.message if result.error else '未知错误'}"
                )
                break
            if result.status == "error" and skill.stage == "deduce":
                report.status = "partial"
                report.stop_reason = (
                    f"{skill.name}: {result.error.message if result.error else '未知错误'}"
                )
                break
            if result.status == "partial":
                report.status = "partial"

        self._settle(report)
        return report

    # --- assembly --------------------------------------------------------
    def _attach(self, report: PipelineReport, stage: str, result: ToolResult) -> None:
        payload = result.data.get("payload")
        if not payload:
            return
        model = {
            "understand": EventFacts,
            "impact": ImpactScope,
            "resource": ResourceAssessment,
            "deduce": DeductionView,
            "audit": AuditReport,
        }[stage]
        value = model.model_validate(payload)
        setattr(
            report,
            {
                "understand": "facts",
                "impact": "impact",
                "resource": "resources",
                "deduce": "deduction",
                "audit": "audit",
            }[stage],
            value,
        )

    def _settle(self, report: PipelineReport) -> None:
        reasons: list[str] = []
        if report.deduction is not None:
            reasons.extend(report.deduction.handoff_reasons)
        for step in report.steps:
            if step.status == "partial":
                reasons.append(f"{step.skill} 降级：{','.join(step.degraded) or '未说明'}")
            if step.status == "error":
                reasons.append(f"{step.skill} 失败：{step.error_code}")
        report.handoff_reasons = _dedupe(reasons)
        report.handoff_required = bool(report.handoff_reasons)
        # 只有一个条件允许对外发布：审计通过且整链没有中断
        report.publishable = (
            report.status == "ok"
            and report.audit is not None
            and report.audit.rejected == 0
            and report.stop_reason is None
        )
        if not report.publishable and report.stop_reason is None:
            report.stop_reason = "存在降级跳，结论需人工确认"

    # --- rendering -------------------------------------------------------
    def render(self, report: PipelineReport) -> str:
        return render_report(report)


# --- manifest for Nexent ------------------------------------------------


def skill_manifest() -> dict[str, Any]:
    """Binding table consumed by the Nexent side.

    Generated from the live objects so a renamed skill or widened request model
    shows up as a diff here instead of as a runtime mismatch in the orchestrator.
    """
    return {
        "manifest_version": SKILL_MANIFEST_VERSION,
        "order": list(SKILL_ORDER),
        "envelope": {
            "status": ["ok", "partial", "error"],
            "data_keys": ["skill", "stage", "payload", "degraded", "notice?"],
            "note": "Skill 与 MCP 工具使用同一 ToolResult 信封；partial 不带 error，缺口原因在 degraded/notice",
        },
        # 反事实不是链路中的一跳，而是对已有结论的追问，因此不进 SKILL_ORDER，
        # 由编排层在拿到基线结论后直接调用。写在这里是为了让绑定表仍然完整。
        "standalone_tools": [
            {
                "name": "counterfactual_run",
                "when": "已有一条可发布的基线结论，用户追问“如果……会怎样”",
                "mcp_tools": ["counterfactual_run"],
                "cases": ["CF1", "CF2", "CF3"],
                "failure_mode": "基线越权或不可计算 -> PERMISSION_DENIED / EVIDENCE_INSUFFICIENT，不给空差异",
            }
        ],
        "skills": [
            {
                "name": skill.name,
                "stage": skill.stage,
                "description": skill.description,
                "request_schema": skill.request_model.model_json_schema(),
                "mcp_tools": list(skill.tools),
                "failure_mode": skill.failure_mode,
            }
            for skill in SKILLS
        ],
    }


# --- helpers ------------------------------------------------------------


def _as_error_code(value: str) -> ErrorCode:
    try:
        return ErrorCode(value)
    except ValueError:
        return ErrorCode.INTERNAL_ERROR


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _dedupe(items: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.add(item)
            ordered.append(item)
    return ordered


def _cited_refs(outcome: DeductionResult) -> list[str]:
    refs: set[str] = set()
    for step in outcome.steps:
        refs.update(step.evidence_refs)
    for conflict in outcome.conflicts:
        refs.update(conflict.evidence_refs)
    for gap in outcome.resource_gaps:
        refs.update(gap.evidence_refs)
    return sorted(refs)


def _scope_from(
    runtime: SkillRuntime,
    data: Mapping[str, Any],
    *,
    seed_twin: str,
    request: ImpactRequest,
) -> ImpactScope:
    triples = data.get("triples") or []
    objects = {row["twin_id"]: row for row in (data.get("objects") or [])}

    predicates: dict[str, set[str]] = {}
    for triple in triples:
        for end in (triple["subject_id"], triple["object_id"]):
            predicates.setdefault(end, set()).add(triple["predicate"])

    affected: list[ImpactItem] = []
    departments: set[str] = set()
    districts: set[str] = set()
    roads: set[str] = set()
    for twin_id, row in objects.items():
        if twin_id == seed_twin:
            continue
        kind = row["kind"]
        if kind == ObjectKind.DEPARTMENT.value:
            departments.add(row["name"])
            continue
        if kind == ObjectKind.REGION.value:
            districts.add(row["name"])
            continue
        if kind not in {item.value for item in IMPACT_KINDS}:
            continue
        via = sorted(predicates.get(twin_id, ()))
        affected.append(
            ImpactItem(
                kind=kind,
                name=row["name"],
                twin_id=twin_id,
                via_predicate=via[0] if via else "",
            )
        )
        if kind == ObjectKind.ROAD.value:
            roads.add(row["name"])

    counts: dict[str, int] = {}
    for item in affected:
        counts[item.kind] = counts.get(item.kind, 0) + 1

    seed = runtime.objects_by_id.get(seed_twin)
    if seed is not None:
        district = runtime.district_of(seed)
        if district:
            districts.add(district)

    affected.sort(key=lambda item: (item.kind, item.name))
    return ImpactScope(
        affected=affected,
        counts_by_kind=counts,
        roads=sorted(roads),
        districts=sorted(districts),
        departments=sorted(departments),
        triple_count=int(data.get("triple_count", len(triples))),
        filtered_reason=data.get("reason"),
        max_hops=request.max_hops,
    )


def render_report(report: PipelineReport) -> str:
    """Plain-text view for the CLI and the Demo layer's log pane."""
    lines: list[str] = []
    lines.append(
        f"事件 {report.event_id} @ {report.as_of.isoformat()}   状态 {report.status}"
        f"   可发布 {'是' if report.publishable else '否'}"
    )
    lines.append(
        f"快照 {report.snapshot_hash[:12]}  知识版本 {report.knowledge_version}"
    )
    lines.append("")

    facts = report.facts
    if facts is not None:
        lines.append("[1 事件理解]")
        lines.append(
            f"  片区 {facts.district or '-'}  道路 {facts.road_id or '-'}  "
            f"深度 {facts.depth_cm if facts.depth_cm is not None else '-'} cm  "
            f"响应 {facts.response_level or '-'}  预警 {facts.warning_level or '-'}"
        )
        if facts.data_gaps:
            for gap in facts.data_gaps:
                lines.append(f"  ! {gap}")

    scope = report.impact
    if scope is not None:
        lines.append("[2 影响分析]")
        counts = "  ".join(f"{kind}={count}" for kind, count in sorted(scope.counts_by_kind.items()))
        lines.append(f"  受影响 {len(scope.affected)} 个对象  {counts or '-'}")
        if scope.roads:
            lines.append(f"  道路: {', '.join(scope.roads)}")
        if scope.filtered_reason:
            lines.append(f"  ! 范围不完整: {scope.filtered_reason}")

    resources = report.resources
    if resources is not None:
        lines.append("[3 资源检查]")
        if resources.unavailable_reason:
            lines.append(f"  ! 无法测算: {resources.unavailable_reason}")
        for line in resources.lines:
            lines.append(
                f"  {line.resource_id}: 需 {line.required} / 可用 {line.available} / 缺口 {line.gap} {line.unit}"
            )
            for item in line.basis:
                lines.append(f"    - {item}")

    view = report.deduction
    if view is not None:
        lines.append("[4 跨部门推演]")
        for stage in view.stages:
            if stage.stage == "action":
                continue
            lines.append(f"  {stage.stage:<10} {stage.status}")
        lines.append("  冲突:")
        for conflict in view.conflicts:
            lines.append(
                f"    {conflict.conflict_id} {conflict.kind.value:<10} {conflict.subject:<12}"
                f" {','.join(conflict.orgs) or '-'}"
            )
        lines.append("  处置顺序:")
        for index, action in enumerate(view.action_order, start=1):
            lines.append(f"    {index}. {action}")
        for risk in view.residual_risks:
            lines.append(f"  二次风险: {risk}")

    audit = report.audit
    if audit is not None:
        lines.append("[5 证据审计]")
        lines.append(
            f"  决策 {audit.decision_id}  claim {audit.supported}/{len(audit.claims)} 通过"
            f"  引用完整率 {audit.citation_completeness:.2f}"
        )
        if audit.record_id:
            lines.append(f"  账本记录 {audit.record_id}  链校验 {'通过' if audit.chain_ok else '失败'}")

    if report.handoff_required:
        lines.append("")
        lines.append("需人工接手:")
        for reason in report.handoff_reasons:
            lines.append(f"  → {reason}")
    if report.stop_reason:
        lines.append("")
        lines.append(f"终止原因: {report.stop_reason}")
    return "\n".join(lines)


# --- CLI ----------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    # CLI JSON must be UTF-8 on Windows so Chinese evidence remains valid.
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    import argparse

    parser = argparse.ArgumentParser(description="five-skill orchestration over the deduction chain")
    parser.add_argument("--event", default=None, help="工单编号，如 WO-20261029-007")
    parser.add_argument("--as-of", default=None, dest="as_of", help="ISO 8601 带时区")
    parser.add_argument("--caller-labels", default="", help="逗号分隔的调用方标签")
    parser.add_argument("--dataset", default=None, help="数据集目录")
    parser.add_argument("--rules", default=str(DEFAULT_RULES_PATH), help="规则文件")
    parser.add_argument("--ledger", default=None, help="溯源账本目录（不存在则新建）")
    parser.add_argument("--no-persist", action="store_true", help="只审计不落账")
    parser.add_argument("--manifest", action="store_true", help="打印 Nexent 绑定表 JSON")
    parser.add_argument("--json", action="store_true", help="输出完整 JSON")
    args = parser.parse_args(argv)

    if args.manifest:
        print(json.dumps(skill_manifest(), ensure_ascii=False, indent=2))
        return 0

    if not args.event or not args.as_of:
        parser.error("需要 --event 与 --as-of（或用 --manifest）")

    runtime = SkillRuntime(args.dataset, rules_path=args.rules, ledger_dir=args.ledger)
    pipeline = SkillPipeline(runtime)
    report = pipeline.run(
        args.event,
        args.as_of,
        caller_labels=[item for item in args.caller_labels.split(",") if item],
        persist=not args.no_persist,
    )
    if args.json:
        print(json.dumps(report.model_dump(mode="json"), ensure_ascii=False, indent=2))
    else:
        print(render_report(report))
    return 0 if report.status != "error" else 1


__all__ = [
    "AuditReport",
    "AuditRequest",
    "DeductionView",
    "EventFacts",
    "ImpactRequest",
    "ImpactScope",
    "PipelineReport",
    "ResourceAssessment",
    "SKILLS",
    "SKILL_BY_NAME",
    "SKILL_BY_STAGE",
    "SKILL_ORDER",
    "Skill",
    "SkillPipeline",
    "SkillRequest",
    "SkillRuntime",
    "render_report",
    "skill_manifest",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

