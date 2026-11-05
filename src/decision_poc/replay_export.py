"""把一次推演导出成"可回放的脚本"：UE / Blender 侧只负责按时序演，不重新推理。

回放最容易做假的地方是"把结论写死在动画里"。这里的做法相反：每一帧的内容都取自
一次真实运行（`SkillPipeline.run`）——六跳的每一跳有什么证据、影响范围里有哪些对象、
处置顺序引用哪条规则，全部来自这次运行的返回值，导出成 `samples/replay/core_run.json`。
引擎侧只是按这份文件高亮、推进、显示文字，它不知道也不需要知道推理逻辑。

字段口径：

| 字段 | 来源 | 用途 |
| --- | --- | --- |
| `steps[].stage/status` | `DeductionResult.steps` | 六跳的进度与是否降级 |
| `steps[].highlights` | `ImpactScope.affected` 的 twin_id | 该跳要亮起来的对象 |
| `steps[].evidence` | 该跳 `evidence_refs` | 界面上点开能看到的依据定位 |
| `actions` | `DeductionResult.action_order` + 规则 id | 处置顺序与出处 |
| `metrics` | 推演与资源测算 | 现场讲解用的三个数字 |

`highlights` 用的是 `twin_id`，和 Blender 对象属性、UE Actor tag 里的 `twin:` 一致，
不存在"动画里亮的是另一个对象"这种错位。
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from pydantic import Field

from .contracts import StrictModel
from .hydro_contracts import HydrographResult
from .skills import SkillPipeline, SkillRuntime
from .urban_dataset import DEFAULT_DATASET_ROOT

REPLAY_VERSION = "urban-twin-replay-v1"
OUTPUT_NAME = "core_run.json"

STAGE_TITLES = {
    "observed": "现象：积水与预警",
    "affected": "受影响对象",
    "owner": "责任部门",
    "conflict": "职责冲突",
    "resource": "资源需求与缺口",
    "action": "处置顺序",
}


class ReplayStep(StrictModel):
    index: int
    stage: str
    title: str
    status: str
    highlights: list[str] = Field(default_factory=list)
    narrative: str = ""
    evidence: list[str] = Field(default_factory=list)


class ReplayAction(StrictModel):
    order: int
    action: str
    rule_ids: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)


class ReplayScript(StrictModel):
    schema_version: str = REPLAY_VERSION
    event_id: str
    as_of: datetime
    district: str | None = None
    snapshot_hash: str = ""
    knowledge_version: str = ""
    steps: list[ReplayStep] = Field(default_factory=list)
    actions: list[ReplayAction] = Field(default_factory=list)
    metrics: dict[str, object] = Field(default_factory=dict)


def build_replay(
    event_id: str,
    as_of: str | datetime,
    *,
    caller_labels: list[str] | None = None,
    runtime: SkillRuntime | None = None,
    hydrograph: HydrographResult | None = None,
) -> ReplayScript:
    runtime = runtime or SkillRuntime()
    labels = list(caller_labels or ["ORG-EMERG"])
    report = SkillPipeline(runtime).run(
        event_id,
        as_of,
        caller_labels=labels,
        persist=False,
        hydrograph=hydrograph,
    )
    # 规则 id 已随同一次 pipeline 的 StageView 返回；不再重复调用引擎。
    return build_replay_from_report(report, None, event_id=event_id, as_of=as_of)


def build_replay_from_report(
    report,
    engine_steps=None,
    *,
    event_id: str,
    as_of: str | datetime,
) -> ReplayScript:
    """从已经跑过的一次 pipeline + 引擎结果组装回放脚本。

    给 `visualize` 复用：可视化层要的是"同一份报告"（还要拿 decision_inputs 算分配），
    重跑一遍 pipeline 会让"快照里的六跳"和"分配用的六跳"来自两次运行。这里把组装
    逻辑单独暴露出来，调用方传同一份报告进来即可。
    """
    deduction = report.deduction
    if engine_steps is None:
        rules_by_action = {
            step.conclusion.get("action", ""): list(step.rule_ids)
            for step in deduction.stages
            if step.stage == "action"
        } if deduction else {}
    else:
        # 兼容旧调用方传入引擎步骤；新路径只使用同一份 pipeline 结果。
        rules_by_action = {
            step.conclusion.get("action", ""): list(step.rule_ids)
            for step in engine_steps
            if step.stage == "action"
        }

    highlights_by_kind: dict[str, list[str]] = {}
    if report.impact:
        for item in report.impact.affected:
            highlights_by_kind.setdefault(item.kind, []).append(item.twin_id)
    road_ids = (
        sorted({item.twin_id for item in report.impact.affected if item.kind == "road"})
        if report.impact
        else []
    )
    # 回放第一跳要亮的是"积水现象"：真源 scene.json 里没有独立的 water patch twin，
    # 水面就是该事件的可见现象，绑定到 incident twin；找不到时不亮假对象（返回空串）。
    water_twin = resolve_flood_twin(event_id)

    steps: list[ReplayStep] = []
    if deduction:
        for index, stage in enumerate(deduction.stages):
            stage_highlights: list[str] = []
            if stage.stage == "observed":
                stage_highlights = [water_twin]
            elif stage.stage in {"affected", "owner", "conflict"}:
                stage_highlights = sorted(
                    {twin_id for ids in highlights_by_kind.values() for twin_id in ids}
                )[:40]
            elif stage.stage == "resource":
                stage_highlights = sorted(
                    {twin_id for kind in ("pump_station", "drainage") for twin_id in highlights_by_kind.get(kind, [])}
                )
            elif stage.stage == "action":
                stage_highlights = road_ids
            steps.append(
                ReplayStep(
                    index=index,
                    stage=stage.stage,
                    title=STAGE_TITLES.get(stage.stage, stage.stage),
                    status=stage.status,
                    highlights=stage_highlights,
                    narrative=_narrative(stage.stage, stage.conclusion, stage.detail),
                    evidence=list(stage.evidence_refs)[:12],
                )
            )

    actions: list[ReplayAction] = []
    if deduction:
        action_stages = [step for step in deduction.stages if step.stage == "action"]
        ordered = sorted(action_stages, key=lambda item: int(item.conclusion.get("order", "99")))
        for order, step in enumerate(ordered, start=1):
            actions.append(
                ReplayAction(
                    order=order,
                    action=step.conclusion.get("action", ""),
                    rule_ids=rules_by_action.get(step.conclusion.get("action", ""), []),
                    evidence=list(step.evidence_refs)[:6],
                )
            )

    metrics: dict[str, object] = {
        "affected": len(report.impact.affected) if report.impact else 0,
        "conflicts": len(deduction.conflicts) if deduction else 0,
        "gap": sum(gap.gap for gap in deduction.resource_gaps) if deduction else 0,
        "publishable": report.publishable,
        "status": report.status,
        "water_twin": water_twin,
    }
    if deduction and deduction.hydrograph is not None:
        metrics["hydrograph"] = deduction.hydrograph.model_dump(mode="json")
    return ReplayScript(
        event_id=event_id,
        as_of=datetime.fromisoformat(as_of) if isinstance(as_of, str) else as_of,
        district=report.facts.district if report.facts else None,
        snapshot_hash=report.snapshot_hash,
        knowledge_version=report.knowledge_version,
        steps=steps,
        actions=actions,
        metrics=metrics,
    )



def resolve_flood_twin(event_id: str, dataset_root: Path | str | None = None) -> str:
    """把"积水现象"解析成真源里真实存在的 twin_id。

    历史实现用 f"TW-water-{event_id}" 拼 id，但 scene.json 的 water 类是管线
    （TW-water-PL-WTR-*），这个拼出来的 id 在任何真源/布局里都不存在，回放第一跳
    会去亮一个不存在的对象。现在的口径：

    1. 优先返回该事件的 incident twin（`TW-incident-<event_id>`）——水面是该事件的
       可见现象，UE 布局里水面 Actor 也挂这个 tag；
    2. 事件 twin 缺失时，回退到真源里第一条 water 类 twin；
    3. 都没有就返回空串，调用方不得再拼 id。
    """
    root = Path(dataset_root) if dataset_root is not None else DEFAULT_DATASET_ROOT
    try:
        scene = json.loads((root / "scene.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    objects = scene.get("objects") or []
    incident_id = f"TW-incident-{event_id}"
    if any(item.get("twin_id") == incident_id for item in objects):
        return incident_id
    water_ids = sorted(
        str(item["twin_id"])
        for item in objects
        if item.get("kind") == "water" and item.get("twin_id")
    )
    return water_ids[0] if water_ids else ""

def _narrative(stage: str, conclusion: dict[str, str], detail: str) -> str:
    """一句话旁白，只从结论字典里取，不额外发挥。"""
    if stage == "observed":
        return (
            f"{conclusion.get('district', '-')} 积水 {conclusion.get('depth_cm', '-')}cm，"
            f"预警 {conclusion.get('warning_level', '无')}，响应等级 {conclusion.get('response_level', '-')}"
        )
    if stage == "affected":
        targets = [name for name in conclusion.get("affected", "").split(",") if name]
        return f"受影响对象 {len(targets)} 个：" + "、".join(targets)
    if stage == "owner":
        return (
            f"统筹 {conclusion.get('coordinator', '-')}；"
            f"未指派 {conclusion.get('unassigned', '无')}"
        )
    if stage == "conflict":
        return (
            f"权限交叉 {conclusion.get('overlap', '-')}；职责空白 {conclusion.get('gap', '-')}；"
            f"资源争用 {conclusion.get('contention', '-')}"
        )
    if stage == "resource":
        return (
            f"需求 {conclusion.get('required', '-')} / 可用 {conclusion.get('available', '-')} / "
            f"缺口 {conclusion.get('gap', '-')}"
        )
    if stage == "action":
        return conclusion.get("action", detail or "-")
    return detail or stage


def write_replay(path: Path | str, script: ReplayScript) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(script.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="export a replayable deduction run")
    parser.add_argument("--event", default="WO-20261029-007")
    parser.add_argument("--as-of", default="2026-10-29T09:40:00+08:00")
    parser.add_argument("--labels", default="ORG-EMERG")
    parser.add_argument("--out", default=str(DEFAULT_DATASET_ROOT.parents[1] / "replay" / OUTPUT_NAME))
    args = parser.parse_args(argv)

    script = build_replay(args.event, args.as_of, caller_labels=args.labels.split(","))
    target = write_replay(args.out, script)
    print(
        f"{target.name}: {len(script.steps)} 跳 / {len(script.actions)} 个动作 / "
        f"受影响 {script.metrics.get('affected')} 个对象，快照 {script.snapshot_hash[:12]}"
    )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
