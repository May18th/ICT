"""Counterfactual branches over the same rules and semantic layer (P6 extension).

The three questions in `docs/urban-scenario-ground-truth.md` §4 (CF1-CF3) have
one property that decides the architecture: **the answer must be replayable**.
So this module never writes "如果先封路，抢修会慢 15 分钟" — it interferes with an
input, rebuilds the world, replays the same five-skill pipeline, and diffs the
conclusions. Every number and every ordering in the output is a computed
difference, and a reviewer can re-run it.

Two kinds of intervention, both on *inputs*:

| kind | 改什么 | 用于 |
| --- | --- | --- |
| `data_patch` | 数据集某一行某列（库存数量、工期起止） | CF2、CF3 |
| `rule_patch` | 规则的一个参数（处置顺序 `then.order`） | CF1 |

Why not patch the deduction logic: a branch that changes the code cannot be
compared with the baseline, because the two runs no longer share a rule set.

Honesty rule inherited from P6: if the current knowledge cannot answer the
question, the result says which rule field is missing instead of producing a
plausible sentence. CF3 is exactly that case — the rules contain no date field,
so shifting a construction plan by a day changes nothing, and the report states
it as 
o_effect` with the reason, not as "风险上调".
"""

from __future__ import annotations

import csv
import json
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import Field

from .contracts import StrictModel
from .deduction import RuleSet
from .governance import ErrorCode
from .road_graph import RoadGraph, RoadGraphUnavailable, TravelImpact
from .semantic import ObjectKind
from .skills import PipelineReport, SkillPipeline, SkillRuntime

# 规则条件里出现这些字段名，才说明工期/时间参与了判定。
DATE_FIELDS: tuple[str, ...] = (
    "start_at",
    "end_at",
    "reported_at",
    "issued_at",
    "valid_from",
    "valid_to",
    "duration_days",
)


class CounterfactualUnavailable(RuntimeError):
    """基线本身算不出来，就没有"反事实"可言。

    这里必须抛而不是回一份空差异：空差异读起来像"没有影响"，而真相是
    "你连基线都没拿到"。两种情况在展示层长得一样，在结论上天差地别。
    """

    def __init__(self, code: ErrorCode, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class InterventionKind(StrEnum):
    DATA_PATCH = "data_patch"
    RULE_PATCH = "rule_patch"


class Intervention(StrictModel):
    kind: InterventionKind
    target: str = Field(min_length=1)  # 文件名 或 rule_id
    field: str = Field(min_length=1)  # CSV 列名 或 规则字段路径（then.order）
    value: str
    match: dict[str, str] = Field(default_factory=dict)  # 定位数据行
    note: str = ""

    def describe(self) -> str:
        where = ",".join(f"{key}={val}" for key, val in sorted(self.match.items()))
        return f"{self.kind.value}:{self.target}[{where}].{self.field}={self.value}"


class CounterfactualCase(StrictModel):
    case_id: str = Field(min_length=1)
    question: str = Field(min_length=1)
    expectation: str = ""
    interventions: list[Intervention] = Field(min_length=1)
    prioritise: bool = False


class ActionMove(StrictModel):
    action: str
    before: int
    after: int


class BranchSummary(StrictModel):
    """What one world concludes — the unit the diff compares."""

    event_id: str
    actions: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    conflict_kinds: list[str] = Field(default_factory=list)
    gaps: dict[str, int] = Field(default_factory=dict)
    total_gap: int = 0
    inventory: int = 0  # 参与测算的抽水泵总量，缺口的分母
    required_total: int = 0
    available_total: int = 0
    # 按片区归集后，同一片区换一个视角推演也是同一个数
    demand_by_district: dict[str, int] = Field(default_factory=dict)
    available_by_district: dict[str, int] = Field(default_factory=dict)
    exposed_warning_level: str = "none"
    exposed_hours: float = 0.0
    risks: list[str] = Field(default_factory=list)
    affected_count: int = 0
    affected_roads: list[str] = Field(default_factory=list)
    stage_status: dict[str, str] = Field(default_factory=dict)
    depth_cm: float | None = None
    closure_required: bool = False
    has_hazard: bool = False
    publishable: bool = False
    status: str = ""
    error_code: str | None = None
    failed_skill: str | None = None
    snapshot_hash: str = ""


class CounterfactualDiff(StrictModel):
    changed: bool = False
    actions_added: list[str] = Field(default_factory=list)
    actions_removed: list[str] = Field(default_factory=list)
    actions_moved: list[ActionMove] = Field(default_factory=list)
    conflicts_added: list[str] = Field(default_factory=list)
    conflicts_removed: list[str] = Field(default_factory=list)
    gaps_delta: dict[str, int] = Field(default_factory=dict)
    risks_added: list[str] = Field(default_factory=list)
    risks_removed: list[str] = Field(default_factory=list)
    affected_delta: int = 0
    publishable_before: bool = True
    publishable_after: bool = True
    snapshot_changed: bool = False


class RoadState(StrictModel):
    road_id: str
    name: str
    status: str
    closed: bool = False
    under_construction: bool = False

    @property
    def passable(self) -> bool:
        return not self.closed and not self.under_construction and self.status == "normal"


class DistrictCase(StrictModel):
    district: str
    event_id: str
    depth_cm: float | None = None
    closure_required: bool = False
    has_hazard: bool = False
    action_count: int = 0
    # 本片区自己的需求与本区库存：跨区调配是"下一步"，不是它的库存
    required_total: int = 0
    available_total: int = 0
    total_gap: int = 0
    world_required: int = 0  # 该次推演里所有片区合计需求，用于对照
    exposed_warning_level: str = "none"
    exposed_hours: float = 0.0
    publishable: bool = False


class PriorityFinding(StrictModel):
    recommended: str | None = None
    ranked: list[DistrictCase] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    cross_district_note: str = ""
    evidence_refs: list[str] = Field(default_factory=list)


class CounterfactualResult(StrictModel):
    case_id: str
    question: str
    expectation: str = ""
    event_id: str
    as_of: datetime
    interventions: list[str] = Field(default_factory=list)
    baseline: BranchSummary
    intervened: BranchSummary
    diff: CounterfactualDiff
    verdict: str = ""
    road_states: list[RoadState] = Field(default_factory=list)
    travel: TravelImpact | None = None
    priority: PriorityFinding | None = None
    uncomputable: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)


# --- cases --------------------------------------------------------------

CASES: dict[str, CounterfactualCase] = {
    "CF1": CounterfactualCase(
        case_id="CF1",
        question="若先封路再隔离风险源，会影响哪些抢修路径？",
        expectation="抢修车辆通行受限，燃气抢修到场时间后移；给出受影响路径集合。",
        interventions=[
            Intervention(
                kind=InterventionKind.RULE_PATCH,
                target="RULE-ACT-ISOLATE-FIRST",
                field="then.order",
                value="3",
                note="隔离风险源后移",
            ),
            Intervention(
                kind=InterventionKind.RULE_PATCH,
                target="RULE-ACT-CLOSE-ROAD",
                field="then.order",
                value="2",
                note="封路提前",
            ),
        ],
    ),
    "CF2": CounterfactualCase(
        case_id="CF2",
        question="若抽水泵只有 2 台（跨区合计），应优先保障哪个区域？",
        expectation="只能保一处：给出 A-03 与 B-01 的取舍及依据，并说明跨区调用时延。",
        prioritise=True,
        interventions=[
            Intervention(
                kind=InterventionKind.DATA_PATCH,
                target="resource_inventory.csv",
                match={"resource_id": "RES-PUMP-M"},
                field="qty_available",
                value="2",
                note="A-03 库存压到 2 台",
            ),
            Intervention(
                kind=InterventionKind.DATA_PATCH,
                target="resource_inventory.csv",
                match={"resource_id": "RES-PUMP-M-B01"},
                field="qty_available",
                value="0",
                note="B-01 库存清零",
            ),
        ],
    ),
    "CF3": CounterfactualCase(
        case_id="CF3",
        question="若 CP-2026-041 施工延期一天，风险是否扩大？",
        expectation="开挖面暴露跨越降雨窗口，二次风险等级上调，需重推跳 5/跳 6。",
        interventions=[
            Intervention(
                kind=InterventionKind.DATA_PATCH,
                target="construction_plans.csv",
                match={"plan_id": "CP-2026-041"},
                field="end_at",
                value="2026-10-30T18:00:00+08:00",
                note="完工顺延一天（开工时间不变：推迟开工会让今日基坑不存在，那是另一个问题）",
            ),
        ],
    ),
}


# --- patching -----------------------------------------------------------


def _read_table(path: Path) -> tuple[list[str], list[list[str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        rows = [row for row in csv.reader(handle) if row]
    if not rows:
        raise ValueError(f"空表: {path.name}")
    header, body = rows[0], rows[1:]
    # 只去掉表头 BOM：正文里的空格是数据
    return [cell.strip() for cell in header], body


def _write_table(path: Path, header: Sequence[str], rows: Sequence[Sequence[str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(list(header))
        writer.writerows([list(row) for row in rows])


def _patch_table(root: Path, interventions: Sequence[Intervention]) -> None:
    grouped: dict[str, list[Intervention]] = {}
    for item in interventions:
        grouped.setdefault(item.target, []).append(item)

    for file_name, items in grouped.items():
        path = root / file_name
        if not path.is_file():
            raise FileNotFoundError(f"数据集缺少文件: {file_name}")
        header, rows = _read_table(path)
        for item in items:
            if item.field not in header:
                raise KeyError(f"{file_name} 没有列 {item.field}")
            if not item.match:
                raise ValueError(f"{item.target} 的补丁缺少行定位条件")
            index_of = {name: header.index(name) for name in item.match}
            hits = 0
            for row in rows:
                if all(row[index_of[key]] == value for key, value in item.match.items()):
                    row[header.index(item.field)] = item.value
                    hits += 1
            if hits != 1:
                raise ValueError(
                    f"补丁必须唯一命中一行，实际命中 {hits} 行: {item.describe()}"
                )
        _write_table(path, header, rows)


def _patch_rules(rules_payload: dict[str, Any], interventions: Sequence[Intervention]) -> dict[str, Any]:
    for item in interventions:
        if item.kind is not InterventionKind.RULE_PATCH:
            continue
        target = next(
            (rule for rule in rules_payload["rules"] if rule["rule_id"] == item.target), None
        )
        if target is None:
            raise KeyError(f"规则不存在: {item.target}")
        *parents, leaf = item.field.split(".")
        node: Any = target
        for key in parents:
            if key not in node:
                raise KeyError(f"规则 {item.target} 没有字段 {item.field}")
            node = node[key]
        if leaf not in node:
            raise KeyError(f"规则 {item.target} 没有字段 {item.field}")
        node[leaf] = item.value
    return rules_payload


class _Branch:
    """A patched copy of the world, cleaned up when the branch ends."""

    def __init__(self, data_root: Path | None, rules_path: Path | None, workdir: Path | None) -> None:
        self.data_root = data_root
        self.rules_path = rules_path
        self._workdir = workdir

    def close(self) -> None:
        if self._workdir is not None and self._workdir.exists():
            shutil.rmtree(self._workdir, ignore_errors=True)

    def __enter__(self) -> "_Branch":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def _prepare(runtime: SkillRuntime, interventions: Sequence[Intervention]) -> _Branch:
    """Copy what needs changing into a temp dir; never touch the sample dataset."""
    data_items = [item for item in interventions if item.kind is InterventionKind.DATA_PATCH]
    rule_items = [item for item in interventions if item.kind is InterventionKind.RULE_PATCH]
    if not data_items and not rule_items:
        return _Branch(None, None, None)

    workdir = Path(tempfile.mkdtemp(prefix="cf-"))
    data_root: Path | None = None
    rules_path: Path | None = None

    if data_items:
        data_root = workdir / "dataset"
        shutil.copytree(runtime.dataset_root, data_root)
        _patch_table(data_root, data_items)

    if rule_items:
        payload = json.loads(Path(runtime.rules_path).read_text(encoding="utf-8"))
        patched = _patch_rules(payload, rule_items)
        rules_path = workdir / "rules.json"
        rules_path.write_text(
            json.dumps(patched, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )

    return _Branch(data_root, rules_path, workdir)


# --- summaries ----------------------------------------------------------


PUMP_KEYWORD = "抽水泵"


def _pump_inventory(runtime: SkillRuntime) -> int:
    total = 0
    for obj in runtime.store.objects():
        if obj.kind is not ObjectKind.RESOURCE:
            continue
        if PUMP_KEYWORD not in obj.attributes.get("name", ""):
            continue
        total += int(_num(obj.attributes.get("qty_available")) or 0)
    return total


def _num(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _summarise(report: PipelineReport, runtime: SkillRuntime) -> BranchSummary:
    deduction = report.deduction
    scope = report.impact
    conflicts = (
        [f"{c.conflict_id}:{c.kind.value}:{c.subject}" for c in deduction.conflicts]
        if deduction
        else []
    )
    gaps = (
        {gap.resource_id: gap.gap for gap in deduction.resource_gaps} if deduction else {}
    )
    actions = list(deduction.action_order) if deduction else []
    kinds = scope.counts_by_kind if scope else {}
    return BranchSummary(
        event_id=report.event_id,
        actions=actions,
        conflicts=conflicts,
        conflict_kinds=sorted({c.kind.value for c in deduction.conflicts}) if deduction else [],
        gaps=gaps,
        total_gap=sum(gaps.values()),
        inventory=_pump_inventory(runtime),
        required_total=report.resources.required_total if report.resources else 0,
        available_total=report.resources.available_total if report.resources else 0,
        demand_by_district=dict(report.resources.demand_by_district) if report.resources else {},
        available_by_district=(
            dict(report.resources.available_by_district) if report.resources else {}
        ),
        exposed_warning_level=report.deduction.exposed_warning_level if report.deduction else "none",
        exposed_hours=report.deduction.exposed_hours if report.deduction else 0.0,
        risks=list(deduction.residual_risks) if deduction else [],
        affected_count=len(scope.affected) if scope else 0,
        affected_roads=list(scope.roads) if scope else [],
        stage_status={step.stage: step.status for step in report.steps},
        depth_cm=report.facts.depth_cm if report.facts else None,
        closure_required=any("封闭积水路段" in action for action in actions),
        has_hazard=bool(kinds.get("gas") or kinds.get("power")),
        publishable=report.publishable,
        status=report.status,
        error_code=next(
            (step.error_code for step in report.steps if step.error_code), None
        ),
        failed_skill=next(
            (step.skill for step in report.steps if step.error_code), None
        ),
        snapshot_hash=report.snapshot_hash,
    )


def _diff(before: BranchSummary, after: BranchSummary) -> CounterfactualDiff:
    before_index = {action: index for index, action in enumerate(before.actions)}
    after_index = {action: index for index, action in enumerate(after.actions)}
    common = set(before_index) & set(after_index)
    moved = [
        ActionMove(action=action, before=before_index[action], after=after_index[action])
        for action in sorted(common, key=lambda item: after_index[item])
        if before_index[action] != after_index[action]
    ]
    gaps_delta = {
        key: after.gaps.get(key, 0) - before.gaps.get(key, 0)
        for key in sorted(set(before.gaps) | set(after.gaps))
        if after.gaps.get(key, 0) != before.gaps.get(key, 0)
    }
    diff = CounterfactualDiff(
        actions_added=sorted(set(after.actions) - set(before.actions)),
        actions_removed=sorted(set(before.actions) - set(after.actions)),
        actions_moved=moved,
        conflicts_added=sorted(set(after.conflicts) - set(before.conflicts)),
        conflicts_removed=sorted(set(before.conflicts) - set(after.conflicts)),
        gaps_delta=gaps_delta,
        risks_added=sorted(set(after.risks) - set(before.risks)),
        risks_removed=sorted(set(before.risks) - set(after.risks)),
        affected_delta=after.affected_count - before.affected_count,
        publishable_before=before.publishable,
        publishable_after=after.publishable,
        snapshot_changed=before.snapshot_hash != after.snapshot_hash,
    )
    # 快照变了但结论没变，是"数据更新、结论稳定"，不算反事实生效
    diff.changed = bool(
        diff.actions_added
        or diff.actions_removed
        or diff.actions_moved
        or diff.conflicts_added
        or diff.conflicts_removed
        or diff.gaps_delta
        or diff.risks_added
        or diff.risks_removed
        or diff.affected_delta
        or diff.publishable_before != diff.publishable_after
    )
    return diff


# --- roads --------------------------------------------------------------


def _road_states(runtime: SkillRuntime, district: str, closed_road: str | None, as_of: datetime) -> list[RoadState]:
    """Passable-road set after the closure, from the road table and the plans."""
    planned: set[str] = set()
    for obj in runtime.store.objects():
        if obj.kind is not ObjectKind.WORK_SITE:
            continue
        if runtime.district_of(obj) != district:
            continue
        road = obj.attributes.get("road_id")
        start = _time(obj.attributes.get("start_at"))
        end = _time(obj.attributes.get("end_at"))
        if road and start and end and start <= as_of <= end:
            planned.add(road)

    states: list[RoadState] = []
    for obj in runtime.store.objects():
        if obj.kind is not ObjectKind.ROAD or runtime.district_of(obj) != district:
            continue
        states.append(
            RoadState(
                road_id=obj.name,
                name=obj.attributes.get("name", ""),
                status=obj.attributes.get("status", "normal"),
                closed=obj.name == closed_road,
                under_construction=obj.name in planned
                or obj.attributes.get("status") == "under_construction",
            )
        )
    states.sort(key=lambda item: item.road_id)
    return states


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


# --- priority (CF2) -----------------------------------------------------


def _involved_districts(runtime: SkillRuntime, report: PipelineReport) -> set[str]:
    """Districts the current event actually touches — including the far end of a
    road whose district differs from the incident's own."""
    districts: set[str] = set()
    if report.facts is not None and report.facts.district:
        districts.add(report.facts.district)
    scope = report.impact
    if scope is not None:
        districts.update(scope.districts)
        for item in scope.affected:
            obj = runtime.objects_by_id.get(item.twin_id)
            district = runtime.district_of(obj) if obj is not None else None
            if district:
                districts.add(district)
    return districts


def _pick_incidents(runtime: SkillRuntime, districts: Sequence[str]) -> dict[str, str]:
    """One worst-case event per district.

    Ranking must compare districts, not individual work orders, so each district
   贡献一个代表事件：积水类优先，其次高严重度。选谁由数据决定，不靠调用方指定。
    """
    wanted = set(districts)
    best: dict[str, tuple[tuple[int, int], str]] = {}
    for obj in runtime.store.objects():
        if obj.kind is not ObjectKind.INCIDENT:
            continue
        if obj.attributes.get("status") not in {None, "open"}:
            continue
        district = runtime.district_of(obj)
        if district not in wanted:
            continue
        score = (
            1 if obj.attributes.get("order_type") == "waterlogging" else 0,
            1 if obj.attributes.get("severity") == "high" else 0,
        )
        if district not in best or score > best[district][0]:
            best[district] = (score, obj.name)
    return {district: pair[1] for district, pair in best.items()}


def _district_cases(
    runtime: SkillRuntime,
    districts: Sequence[str],
    as_of: datetime,
    labels: Sequence[str],
) -> list[DistrictCase]:
    """Run each involved district's own event under the same (scarce) world.

    Priority is then a property of the results, not of a sentence: the district
    that crosses a mandatory threshold and carries a secondary hazard ranks
    first, whichever one the caller happens to be looking at.
    """
    cases: list[DistrictCase] = []
    for district, event_id in sorted(_pick_incidents(runtime, districts).items()):
        branch = SkillPipeline(runtime).run(event_id, as_of, caller_labels=labels, persist=False)
        summary = _summarise(branch, runtime)
        # 只比较"这个片区自己要多少"和"这个片区有多少"：把别区的库存算进它的
        # 分母，会让同一个片区换一个视角就换一个缺口，跨区结论也就没法复算。
        own = summary.demand_by_district.get(district, 0)
        local = summary.available_by_district.get(district, 0)
        cases.append(
            DistrictCase(
                district=district,
                event_id=event_id,
                depth_cm=summary.depth_cm,
                closure_required=summary.closure_required,
                has_hazard=summary.has_hazard,
                action_count=len(summary.actions),
                required_total=own,
                available_total=local,
                total_gap=max(0, own - local),
                world_required=summary.required_total,
                publishable=summary.publishable,
            )
        )
    return cases


def _priority(
    runtime: SkillRuntime, cases: Sequence[DistrictCase], closure_threshold_cm: float
) -> PriorityFinding:
    ranked = sorted(
        cases,
        key=lambda case: (
            case.closure_required,
            case.has_hazard,
            case.depth_cm or 0.0,
            case.action_count,
        ),
        reverse=True,
    )
    reasons: list[str] = []
    if ranked:
        top = ranked[0]
        depth = f"{top.depth_cm:g}cm" if top.depth_cm is not None else "未知深度"
        reasons.append(
            f"{top.district} 积水 {depth}，"
            + ("已过封路阈值" if top.closure_required else "未过封路阈值")
            + f"（阈值 {closure_threshold_cm:g}cm）"
        )
        if top.has_hazard:
            reasons.append(f"{top.district} 受影响范围含燃气/电力管线，存在次生风险")
        for case in ranked[1:]:
            depth_text = f"{case.depth_cm:g}cm" if case.depth_cm is not None else "未知深度"
            reasons.append(
                f"{case.district} 积水 {depth_text}，"
                + ("已过阈值" if case.closure_required else "未过阈值")
                + "，且无次生危险源" * (not case.has_hazard)
            )

    note = ""
    refs: list[str] = []
    for obj in runtime.store.objects():
        if obj.kind is not ObjectKind.RESOURCE:
            continue
        capacity = obj.attributes.get("capacity_note", "")
        if "跨区" in capacity:
            note = f"{obj.name}: {capacity}"
            for triple in runtime.store.triples():
                if triple.involves(obj.twin_id):
                    refs.extend(triple.evidence_refs)
            break

    return PriorityFinding(
        recommended=ranked[0].district if ranked else None,
        ranked=list(ranked),
        reasons=reasons,
        cross_district_note=note,
        evidence_refs=sorted(set(refs)),
    )


# --- rules gap ----------------------------------------------------------


def _rule_fields(rules: RuleSet) -> set[str]:
    fields: set[str] = set()
    for rule in rules.rules:
        for key in rule.when:
            name = key
            for prefix in ("or_", "min_", "max_"):
                if name.startswith(prefix):
                    name = name[len(prefix):]
                    break
            fields.add(name)
    return fields


def _missing_rule_field(runtime: SkillRuntime, wanted: Sequence[str]) -> list[str]:
    present = _rule_fields(runtime.rules)
    return [name for name in wanted if name not in present]


# --- run ----------------------------------------------------------------


def run_counterfactual(
    case_id: str,
    *,
    event_id: str,
    as_of: datetime | str,
    caller_labels: Sequence[str] = (),
    runtime: SkillRuntime | None = None,
) -> CounterfactualResult:
    if case_id not in CASES:
        raise KeyError(f"未知反事实用例: {case_id}")
    case = CASES[case_id]
    base_runtime = runtime or SkillRuntime()
    moment = as_of if isinstance(as_of, datetime) else datetime.fromisoformat(as_of)
    labels = list(caller_labels)

    baseline_report = SkillPipeline(base_runtime).run(
        event_id, moment, caller_labels=labels, persist=False
    )
    baseline = _summarise(baseline_report, base_runtime)
    if baseline.error_code:
        # 基线失败（事件不存在 / 越权 / 参数不可用）时不允许继续：分支只会更空
        raise CounterfactualUnavailable(
            _as_error_code(baseline.error_code),
            f"基线不可比较：{baseline.failed_skill or '基线'} 返回 {baseline.error_code}",
        )

    with _prepare(base_runtime, case.interventions) as branch:
        patched = SkillRuntime(
            branch.data_root or base_runtime.dataset_root,
            rules_path=branch.rules_path or base_runtime.rules_path,
            knowledge_version=base_runtime.knowledge_version,
        )
        intervened_report = SkillPipeline(patched).run(
            event_id, moment, caller_labels=labels, persist=False
        )
        intervened = _summarise(intervened_report, patched)
        diff = _diff(baseline, intervened)

        road_states: list[RoadState] = []
        travel: TravelImpact | None = None
        priority: PriorityFinding | None = None
        uncomputable: list[str] = []
        gap_notes: list[str] = []

        if case.case_id == "CF1":
            district = baseline_report.facts.district if baseline_report.facts else None
            closed_road = baseline_report.facts.road_id if baseline_report.facts else None
            if district:
                road_states = _road_states(
                    base_runtime,
                    district,
                    closed_road=closed_road,
                    as_of=moment,
                )
            if closed_road:
                try:
                    travel = RoadGraph.load(base_runtime.dataset_root).impact(closed_road)
                except RoadGraphUnavailable as exc:
                    uncomputable.append(f"通行时间与绕行里程：{exc}")
            else:
                uncomputable.append("通行时间与绕行里程：事件里没有封闭路段，无比较对象")
            uncomputable.append("门到门到场时间：缺抢修驻地坐标与实时路况，只算到路口之间的通行时间")
            uncomputable.append("跨区路径：A-03 与 B-01 的路段之间没有连接数据，跨区调配只有台账里的 40 分钟")
        elif case.case_id == "CF2":
            districts = sorted(_involved_districts(patched, intervened_report))
            cases = _district_cases(patched, districts, moment, labels)
            priority = _priority(base_runtime, cases, closure_threshold_cm=25.0)
        elif case.case_id == "CF3":
            # 能不能回答这个反事实，取决于规则里有没有"暴露期×预警窗口"的判据
            exposure_rules = [
                rule.rule_id
                for rule in base_runtime.rules.rules
                if "pit_exposed_warning_level" in rule.when
            ]
            if exposure_rules:
                gap_notes.append("判定依据: " + ",".join(exposure_rules))
            else:
                gap_notes.append(
                    "规则集里没有以“开挖面暴露期预警等级”为条件的规则，工期变化不参与判定"
                )
                uncomputable.append("工期跨越降雨窗口的风险判定：缺“暴露期×预警重叠”规则")
            uncomputable.append(
                "超出预警发布时效的工期无法判断：气象侧只有到 10-30 12:00 的预警，更远期无数据"
            )

        verdict = _verdict(case, baseline, intervened, diff, priority, gap_notes, road_states, travel)

        evidence: set[str] = set()
        for step in intervened_report.deduction.stages if intervened_report.deduction else []:
            evidence.update(step.evidence_refs)
        if priority is not None:
            evidence.update(priority.evidence_refs)
        if travel is not None:
            evidence.update(travel.evidence_refs)

    return CounterfactualResult(
        case_id=case.case_id,
        question=case.question,
        expectation=case.expectation,
        event_id=event_id,
        as_of=moment,
        interventions=[item.describe() for item in case.interventions],
        baseline=baseline,
        intervened=intervened,
        diff=diff,
        verdict=verdict,
        road_states=road_states,
        travel=travel,
        priority=priority,
        uncomputable=uncomputable,
        evidence_refs=sorted(evidence)[:40],
    )


def _verdict(
    case: CounterfactualCase,
    before: BranchSummary,
    after: BranchSummary,
    diff: CounterfactualDiff,
    priority: PriorityFinding | None,
    gap_notes: Sequence[str],
    road_states: Sequence[RoadState],
    travel: TravelImpact | None = None,
) -> str:
    """结论只由算出的数值拼装，不写任何算不出来的因果。

    "燃气抢修会慢 15 分钟"这类句子在这里永远不该出现：没有路网和车速，
    这句话既不能算也不能验，答辩时一被追问就崩。现在有了拓扑，慢多少是从
    `TravelImpact` 里读的——它也只算到"路口到路口"，门到门仍然标算不出。
    """
    if case.case_id == "CF1":
        changed = diff.changed
        parts: list[str] = []
        if changed:
            moved = {item.action: (item.before + 1, item.after + 1) for item in diff.actions_moved}
            isolate = next((key for key in moved if "隔离风险源" in key), None)
            closure = next((key for key in moved if "封闭积水路段" in key), None)
            if closure and isolate:
                parts.append(
                    f"封路由第 {moved[closure][0]} 位提前到第 {moved[closure][1]} 位，"
                    f"隔离风险源由第 {moved[isolate][0]} 位后移到第 {moved[isolate][1]} 位"
                )
        else:
            parts.append("调整顺序后处置序列未发生变化，该干预在当前规则下无效")
        passable = [road.name for road in road_states if road.passable]
        blocked = [
            f"{road.name}({'封闭' if road.closed else '施工'})"
            for road in road_states
            if not road.passable
        ]
        if passable:
            parts.append(f"封路后可通行道路为 {'、'.join(passable)}")
        if blocked:
            parts.append(f"受限道路 {'、'.join(blocked)}")
        if travel is not None:
            parts.append(travel.describe())
        return "；".join(parts) + "。"

    if case.case_id == "CF2":
        pumps_before = before.inventory
        pumps_after = after.inventory
        gap_before = before.gaps.get("RES-PUMP-M", 0)
        gap_after = after.gaps.get("RES-PUMP-M", 0)
        head = f"抽水泵 {pumps_before} 台 → {pumps_after} 台，缺口 {gap_before} 台 → {gap_after} 台"
        if priority and priority.recommended:
            demands = "、".join(
                f"{case.district} 需 {case.required_total} 台（本区 {case.available_total} 台，缺 {case.total_gap}）"
                for case in priority.ranked
            )
            head += f"；{demands}，优先保障 {priority.recommended}"
            if priority.ranked and all(
                case.required_total > after.available_total for case in priority.ranked
            ):
                head += (
                    f"；可调合计 {after.available_total} 台低于任一片区单独需求，"
                    "任何分配都不够，需请求市级或邻区增援"
                )
            if priority.reasons:
                head += f"：{priority.reasons[0]}"
            if priority.cross_district_note:
                head += f"；跨区调配代价：{priority.cross_district_note}"
        return head + "。"

    if case.case_id == "CF3":
        if not diff.changed:
            reason = gap_notes[0] if gap_notes else "规则未引用工期字段"
            return (
                f"工期顺延未改变任何推演结论：{reason}。"
                "该问题在当前知识下不可判定——需要补规则与气象数据，而不是补一段结论。"
            )
        if before.exposed_warning_level != after.exposed_warning_level:
            detail = (
                f"开挖面暴露期与预警窗口的重叠等级由 {before.exposed_warning_level} 上调为 "
                f"{after.exposed_warning_level}（重叠 {before.exposed_hours:g}h → {after.exposed_hours:g}h）"
            )
            if diff.risks_added:
                # 只取风险标题（冒号前），整段规则文本放进 diff 里，不塞进一句话结论
                detail += f"；新增二次风险：{diff.risks_added[0].split('：')[0]}"
            return detail + "。"
        changes = len(diff.actions_added) + len(diff.actions_removed) + len(diff.actions_moved)
        return f"工期变更产生 {changes} 处结论变化。"

    return "结论变化 " + ("是" if diff.changed else "否")


def _as_error_code(value: str) -> ErrorCode:
    try:
        return ErrorCode(value)
    except ValueError:
        return ErrorCode.INTERNAL_ERROR


# --- CLI ----------------------------------------------------------------


def _format(result: CounterfactualResult) -> str:
    lines: list[str] = []
    lines.append(f"[{result.case_id}] {result.question}")
    lines.append(f"事件 {result.event_id} @ {result.as_of.isoformat()}")
    lines.append("干预: " + "; ".join(result.interventions))
    lines.append("")
    lines.append(
        f"基线: 动作 {len(result.baseline.actions)} 项 / 冲突 {len(result.baseline.conflicts)} 项 / "
        f"缺口 {result.baseline.total_gap} / 可发布 {'是' if result.baseline.publishable else '否'}"
    )
    lines.append(
        f"反事实: 动作 {len(result.intervened.actions)} 项 / 冲突 {len(result.intervened.conflicts)} 项 / "
        f"缺口 {result.intervened.total_gap} / 可发布 {'是' if result.intervened.publishable else '否'}"
    )
    lines.append("")
    diff = result.diff
    if not diff.changed:
        lines.append("差异: 无（结论层面完全一致）")
    for item in diff.actions_moved:
        lines.append(f"  顺序: {item.action[:28]}… 第 {item.before + 1} → 第 {item.after + 1} 位")
    for action in diff.actions_added:
        lines.append(f"  + 动作: {action[:40]}")
    for action in diff.actions_removed:
        lines.append(f"  - 动作: {action[:40]}")
    for key, delta in diff.gaps_delta.items():
        lines.append(f"  缺口: {key} {delta:+d} 台")
    for item in diff.risks_added:
        lines.append(f"  + 风险: {item[:50]}")
    for item in diff.risks_removed:
        lines.append(f"  - 风险: {item[:50]}")
    if result.baseline.exposed_warning_level != result.intervened.exposed_warning_level:
        lines.append(
            f"  暴露: {result.baseline.exposed_warning_level} "
            f"{result.baseline.exposed_hours:g}h → {result.intervened.exposed_warning_level} "
            f"{result.intervened.exposed_hours:g}h"
        )
    if diff.conflicts_added or diff.conflicts_removed:
        lines.append(f"  冲突: +{len(diff.conflicts_added)} / -{len(diff.conflicts_removed)}")
    if diff.snapshot_changed and not diff.changed:
        lines.append("  （数据版本已变，但结论未变）")

    if result.road_states:
        lines.append("")
        lines.append("道路可通行性:")
        for road in result.road_states:
            mark = "可通行" if road.passable else ("封闭" if road.closed else "受施工影响")
            lines.append(f"  {road.road_id} {road.name}: {mark}")

    if result.travel is not None:
        lines.append("")
        lines.append("通行代价（自由流，不含路口延误）:")
        lines.append("  " + result.travel.describe())
        if result.travel.baseline is not None:
            lines.append("  基线路径: " + result.travel.baseline.describe())
        if result.travel.after_closure is not None:
            lines.append("  封路后路径: " + result.travel.after_closure.describe())

    if result.priority is not None:
        lines.append("")
        lines.append("片区优先级:")
        for case in result.priority.ranked:
            depth = f"{case.depth_cm:g}cm" if case.depth_cm is not None else "-"
            lines.append(
                f"  {case.district} ({case.event_id}) 深度 {depth} 封路 {'是' if case.closure_required else '否'} "
                f"危险源 {'有' if case.has_hazard else '无'} "
                f"需 {case.required_total} 台 / 本区 {case.available_total} 台 / 缺口 {case.total_gap}"
            )
        for reason in result.priority.reasons:
            lines.append(f"  · {reason}")
        if result.priority.cross_district_note:
            lines.append(f"  · {result.priority.cross_district_note}")

    lines.append("")
    lines.append("结论: " + result.verdict)
    if result.uncomputable:
        lines.append("无法计算的部分:")
        for item in result.uncomputable:
            lines.append(f"  ! {item}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    # Keep JSON output valid on Windows consoles with a legacy code page.
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    import argparse

    parser = argparse.ArgumentParser(description="counterfactual branches (CF1-CF3)")
    parser.add_argument("--case", default="all", help="CF1 / CF2 / CF3 / all")
    parser.add_argument("--event", default="WO-20261029-007")
    parser.add_argument("--as-of", default="2026-10-29T09:40:00+08:00", dest="as_of")
    parser.add_argument("--caller-labels", default="ORG-EMERG")
    parser.add_argument("--dataset", default=None)
    parser.add_argument("--rules", default=None)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)

    selected = list(CASES) if args.case.lower() == "all" else [args.case.upper()]
    runtime = SkillRuntime(args.dataset, **({"rules_path": args.rules} if args.rules else {}))
    results = []
    for case_id in selected:
        try:
            results.append(
                run_counterfactual(
                    case_id,
                    event_id=args.event,
                    as_of=args.as_of,
                    caller_labels=[item for item in args.caller_labels.split(",") if item],
                    runtime=runtime,
                )
            )
        except CounterfactualUnavailable as exc:
            print(f"[{case_id}] 拒绝：{exc.message}")
            return 2
    if args.json:
        print(json.dumps([item.model_dump(mode="json") for item in results], ensure_ascii=False, indent=2))
    else:
        print("\n\n".join(_format(result) for result in results))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

