"""P10 可视化工具层：把"决策动作"变成"引擎能演的渲染作业"。

分层（详见 `docs/p10-visualization-tools-design.md`）：

    P6 六跳 + P9 分配策略
          │  组装（不新增推理）
          ▼
    VisualSnapshot   一份可寻址快照，snapshot_id 由内容哈希得出
          │  scene_render
          ▼
    RenderJob        engine-neutral 演出单：场景状态 / 每步高亮 / 面板文本
          │  GET /api/scene/<snapshot_id>
          ▼
    UE  urban_twin_state.apply_job()

三条硬约束，违反任意一条就等于退化成"三维演示"：

1. **只接受快照 id，不接受低层渲染参数**（位姿/材质/镜头）。开放低层参数会让决策
   循环被渲染细节污染，演示也不可复现。
2. **异步、可降级**。作业落盘即返回，不等引擎渲染完成；引擎没起只体现为
   `delivery=pending`，决策层照常出结论。
3. **同一决策同一 id**。重跑逐位一致，"渲染的是不是刚才那份结论"可当场核对。

本模块不写"渲染完成"这种话：它不持有渲染器连接，认领状态只能由引擎侧写回。
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from pydantic import Field

from .allocation import (
    AllocationPolicy,
    AllocationUnavailable,
    decide,
    default_policy,
)
from .contracts import StrictModel
from .replay_export import ReplayAction, ReplayStep, build_replay_from_report
from .scene_export import build_scene_payload
from .skills import SkillPipeline, SkillRuntime

VISUAL_SCHEMA = "urban-twin-visual-snapshot-v1"
JOB_SCHEMA = "urban-twin-render-job-v1"
COMPARE_SCHEMA = "urban-twin-scene-compare-v1"
ACK_SCHEMA = "urban-twin-render-ack-v1"

SCENARIOS: tuple[str, ...] = ("normal", "flood", "closure", "construction")

# 渲染作业默认落在仓库的 artifacts（已 gitignore），可用环境变量改写。
DEFAULT_VISUAL_ROOT = Path(__file__).resolve().parents[2] / "artifacts" / "visual"
ASSET_MANIFEST = (
    Path(__file__).resolve().parents[2]
    / "visualization"
    / "blender"
    / "scenes"
    / "asset_manifest.json"
)

DEFAULT_EVENT_ID = "WO-20261029-007"
DEFAULT_AS_OF = "2026-10-29T09:40:00+08:00"

DEFAULT_LAYOUT_PATH = (
    Path(__file__).resolve().parents[2]
    / "samples"
    / "assets"
    / "urban"
    / "scene_layout.json"
)

_LAYOUT_CACHE: dict[str, dict[str, Any]] = {}


def load_layout(path: Path | str | None = None) -> dict[str, Any]:
    """读引擎中立布局（Blender/UE 渲染用的同一份），带缓存。

    路网的事件状态（closed / under_construction）只在这份文件里；语义层存的是
    物理状态，两者不能互相替代。
    """
    target = Path(path) if path is not None else DEFAULT_LAYOUT_PATH
    key = str(target)
    if key not in _LAYOUT_CACHE:
        _LAYOUT_CACHE[key] = json.loads(target.read_text(encoding="utf-8"))
    return _LAYOUT_CACHE[key]


# --- 路径 ----------------------------------------------------------------


def visual_root(root: Path | str | None = None) -> Path:
    if root is not None:
        return Path(root)
    override = os.environ.get("URBAN_TWIN_VISUAL_ROOT")
    return Path(override) if override else DEFAULT_VISUAL_ROOT


def snapshot_path(snapshot_id: str, root: Path | str | None = None) -> Path:
    return visual_root(root) / "snapshots" / f"{snapshot_id}.json"


def job_path(snapshot_id: str, root: Path | str | None = None) -> Path:
    return visual_root(root) / "jobs" / f"{snapshot_id}.json"


def ack_path(snapshot_id: str, root: Path | str | None = None) -> Path:
    return visual_root(root) / "acks" / f"{snapshot_id}.json"


def tool_log_path(root: Path | str | None = None) -> Path:
    return visual_root(root) / "tool_log.jsonl"


# --- 快照 ----------------------------------------------------------------


class VisualSnapshot(StrictModel):
    """一次决策的可视化快照。全部字段来自当次运行，不含时间戳以免逐位漂移。"""

    schema_version: str = VISUAL_SCHEMA
    snapshot_id: str
    event_id: str
    as_of: datetime
    caller_labels: list[str] = Field(default_factory=list)
    policy: dict[str, Any] = Field(default_factory=dict)
    policy_version: str = "v1"
    scenario: str = "flood"
    snapshot_hash: str = ""
    scene_hash: str = ""
    knowledge_version: str = ""
    steps: list[ReplayStep] = Field(default_factory=list)
    actions: list[ReplayAction] = Field(default_factory=list)
    # 分配方案为 None 表示推演没产出决策输入：显式留空，不伪造"各片区 0 台"
    allocation: dict[str, Any] | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @property
    def highlight_union(self) -> list[str]:
        seen: set[str] = set()
        for step in self.steps:
            seen.update(step.highlights)
        return sorted(seen)


def _as_of_datetime(as_of: str | datetime) -> datetime:
    return datetime.fromisoformat(as_of) if isinstance(as_of, str) else as_of


def _resolve_policy(policy: AllocationPolicy | Mapping[str, Any] | None) -> AllocationPolicy:
    if policy is None:
        return default_policy()
    if isinstance(policy, AllocationPolicy):
        return policy
    return AllocationPolicy.model_validate({"policy_version": "v1", **dict(policy)})


def snapshot_id_for(
    *,
    event_id: str,
    as_of: datetime,
    policy: AllocationPolicy,
    snapshot_hash: str,
) -> str:
    """内容哈希：同一决策 + 同一策略 → 同一 id；换策略 → 换 id。"""
    payload = {
        "event_id": event_id,
        "as_of": as_of.isoformat(),
        "policy": policy.model_dump(mode="json"),
        "snapshot_hash": snapshot_hash,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "snap-" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def pick_scenario(report, layout: dict | None = None) -> str:
    """从事件事实 + 引擎中立布局推一个 UE 能认的场景名，不做美术判断。

    注意数据源：路网的**事件状态**（哪条路封了、哪段在施工）只在
    `scene_layout.json` 里；语义层的 `lifecycle_state` 是物理状态（in_service 等），
    从它推场景会永远得到 flood。四套预设里 construction 是 closure 的超集
    （红色封路 + 黄色施工段都画），所以同时存在时取 construction。
    """
    facts = report.facts
    depth = facts.depth_cm if facts else None
    if depth is None or depth <= 0:
        return "normal"
    layout = layout if layout is not None else load_layout()
    district = facts.district if facts else None
    states = {
        road.get("state", "")
        for road in layout.get("roads", [])
        if district is None or road.get("district") == district
    }
    if "under_construction" in states:
        return "construction"
    if "closed" in states:
        return "closure"
    return "flood"


def build_visual_snapshot(
    event_id: str = DEFAULT_EVENT_ID,
    as_of: str | datetime = DEFAULT_AS_OF,
    *,
    caller_labels: Sequence[str] | None = None,
    policy: AllocationPolicy | Mapping[str, Any] | None = None,
    runtime: SkillRuntime | None = None,
) -> VisualSnapshot:
    """跑一次决策并组装快照。只跑一遍 pipeline，六跳与分配共用同一份报告。"""
    labels = list(caller_labels or ["ORG-EMERG"])
    as_of_dt = _as_of_datetime(as_of)
    runtime = runtime or SkillRuntime()
    resolved = _resolve_policy(policy)

    report = SkillPipeline(runtime).run(event_id, as_of_dt, caller_labels=labels, persist=False)
    engine_steps = runtime.engine.deduce(event_id, as_of=as_of_dt, caller_labels=labels).steps
    script = build_replay_from_report(report, engine_steps, event_id=event_id, as_of=as_of_dt)

    warnings: list[str] = []
    allocation: dict[str, Any] | None = None
    metrics: dict[str, Any] = dict(script.metrics)
    decision_inputs = report.deduction.decision_inputs if report.deduction else {}
    if decision_inputs:
        try:
            decision = decide(
                decision_inputs, resolved, snapshot_hash=report.snapshot_hash
            )
        except AllocationUnavailable as exc:
            warnings.append(str(exc))
        else:
            allocation = decision.model_dump(mode="json")
            metrics.update(
                {
                    "allocated": decision.total_allocated,
                    "demand": decision.total_demand,
                    "unmet": decision.total_unmet,
                    "transfers": len(decision.transfers),
                    "policy_version": resolved.policy_version,
                }
            )
    else:
        warnings.append("推演未产出片区决策输入，本次快照不含分配方案，不能用于对比策略")

    scene_payload = build_scene_payload(runtime.store)
    scene_hash = hashlib.sha256(
        json.dumps(scene_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    snapshot_id = snapshot_id_for(
        event_id=event_id,
        as_of=as_of_dt,
        policy=resolved,
        snapshot_hash=report.snapshot_hash,
    )
    return VisualSnapshot(
        snapshot_id=snapshot_id,
        event_id=event_id,
        as_of=as_of_dt,
        caller_labels=labels,
        policy=resolved.model_dump(mode="json"),
        policy_version=resolved.policy_version,
        scenario=pick_scenario(report),
        snapshot_hash=report.snapshot_hash,
        scene_hash=scene_hash,
        knowledge_version=report.knowledge_version,
        steps=list(script.steps),
        actions=list(script.actions),
        allocation=allocation,
        metrics=metrics,
        warnings=warnings,
    )


# --- 渲染作业 ------------------------------------------------------------


class RenderStep(StrictModel):
    index: int
    stage: str
    title: str
    status: str
    highlights: list[str] = Field(default_factory=list)
    narrative: str = ""
    evidence: list[str] = Field(default_factory=list)


class RenderAction(StrictModel):
    order: int
    action: str
    rule_ids: list[str] = Field(default_factory=list)


class RenderJob(StrictModel):
    """UE 侧唯一需要认的结构。字段名与 `urban_twin_state.apply_job` 对齐。"""

    schema_version: str = JOB_SCHEMA
    job_id: str
    snapshot_id: str
    status: str = "pending"
    scene: dict[str, Any] = Field(default_factory=dict)
    panel: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


def _allocation_lines(snapshot: VisualSnapshot) -> list[str]:
    allocation = snapshot.allocation or {}
    lines: list[str] = []
    for item in allocation.get("lines", []):
        lines.append(
            f"{item['district']} 分配 {item['allocated']} 台"
            f"（需求 {item['demand']}，本区 {item['local_available']}，未满足 {item['unmet']}）"
        )
    for transfer in allocation.get("transfers", []):
        lines.append(
            f"调拨 {transfer['from_district']} → {transfer['to_district']} "
            f"{transfer['qty']} 台（{transfer['delay_min']} 分钟）"
        )
    if not lines:
        lines.append("本次快照无分配方案（推演未产出片区决策输入）")
    return lines


def build_render_job(snapshot: VisualSnapshot) -> RenderJob:
    allocation = snapshot.allocation or {}
    summary_bits = [
        f"事件 {snapshot.event_id}",
        f"策略 {snapshot.policy_version}",
    ]
    if allocation:
        summary_bits.append(
            f"分配 {allocation.get('total_allocated', 0)}/{allocation.get('total_demand', 0)} 台"
            f"，未满足 {allocation.get('total_unmet', 0)} 台"
            f"，跨区调拨 {len(allocation.get('transfers', []))} 次"
        )
    else:
        summary_bits.append("无分配方案")
    summary = "｜".join(summary_bits)

    panel_text = "\n".join(
        [summary, "—" * 12, *_allocation_lines(snapshot)]
    )
    return RenderJob(
        job_id=f"render-{snapshot.snapshot_id}",
        snapshot_id=snapshot.snapshot_id,
        scene={
            "scene_hash": snapshot.scene_hash,
            "snapshot_hash": snapshot.snapshot_hash,
            # P11 补：作业要自述"演的是哪个工单"，渲染执行器据此挑预渲染资产
            "event_id": snapshot.event_id,
            "as_of": snapshot.as_of.isoformat(),
            "scenario": snapshot.scenario,
            "policy_version": snapshot.policy_version,
            "water_twin": snapshot.metrics.get("water_twin"),
            "steps": [step.model_dump(mode="json") for step in snapshot.steps],
            "actions": [action.model_dump(mode="json") for action in snapshot.actions],
        },
        panel={
            "title": f"{snapshot.event_id} · {snapshot.policy_version}",
            "summary": summary,
            "allocation_lines": _allocation_lines(snapshot),
            "text": panel_text,
        },
        metrics=dict(snapshot.metrics),
        warnings=list(snapshot.warnings),
    )


# --- 存取 ----------------------------------------------------------------


def _write_json(path: Path, payload: Any) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return path


def save_snapshot(snapshot: VisualSnapshot, root: Path | str | None = None) -> Path:
    return _write_json(
        snapshot_path(snapshot.snapshot_id, root), snapshot.model_dump(mode="json")
    )


def load_snapshot(snapshot_id: str, root: Path | str | None = None) -> VisualSnapshot:
    path = snapshot_path(snapshot_id, root)
    if not path.is_file():
        raise KeyError(f"未知快照: {snapshot_id}")
    return VisualSnapshot.model_validate_json(path.read_text(encoding="utf-8"))


def save_job(job: RenderJob, root: Path | str | None = None) -> Path:
    return _write_json(job_path(job.snapshot_id, root), job.model_dump(mode="json"))


def load_job(snapshot_id: str, root: Path | str | None = None) -> RenderJob:
    path = job_path(snapshot_id, root)
    if not path.is_file():
        raise KeyError(f"未知渲染作业: {snapshot_id}")
    return RenderJob.model_validate_json(path.read_text(encoding="utf-8"))


def acked(snapshot_id: str, root: Path | str | None = None) -> bool:
    return ack_path(snapshot_id, root).is_file()


def write_ack(
    snapshot_id: str,
    *,
    engine: str = "unreal",
    root: Path | str | None = None,
    artifact: Mapping[str, Any] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> Path:
    """写认领回执。

    `artifact` 是 P11 加的：真渲染器（Blender/UE）出片后要能自证"渲了什么、渲出来的是哪一版"。
    里面只放产物事实（路径/大小/sha256/耗时），不放位姿/材质/镜头这类低层参数——
    和 `scene_render` 的入参红线保持一致。
    """
    payload: dict[str, Any] = {
        "schema_version": ACK_SCHEMA,
        "snapshot_id": snapshot_id,
        "engine": engine,
        "acked_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }
    if artifact:
        payload["artifact"] = dict(artifact)
    if extra:
        payload.update(dict(extra))
    return _write_json(ack_path(snapshot_id, root), payload)


def read_ack(snapshot_id: str, root: Path | str | None = None) -> dict[str, Any] | None:
    path = ack_path(snapshot_id, root)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None



def latest_snapshot_id(root: Path | str | None = None) -> str | None:
    """按修改时间取最近一次的作业。演示现场就靠它，不需要记 id。"""
    directory = visual_root(root) / "jobs"
    if not directory.is_dir():
        return None
    jobs = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime)
    return jobs[-1].stem if jobs else None


def delivery_state(snapshot_id: str, root: Path | str | None = None) -> str:
    if acked(snapshot_id, root):
        return "acked"
    return "pending"


# --- 对比 ----------------------------------------------------------------


class DistrictDelta(StrictModel):
    district: str
    baseline: int = 0
    candidate: int = 0
    delta: int = 0


class CompareResult(StrictModel):
    schema_version: str = COMPARE_SCHEMA
    baseline_id: str
    candidate_id: str
    same_snapshot: bool = False
    policy: dict[str, Any] = Field(default_factory=dict)
    allocation: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    scene: dict[str, Any] = Field(default_factory=dict)
    verdict: str = ""
    warnings: list[str] = Field(default_factory=list)


def _district_deltas(
    baseline: dict[str, Any] | None, candidate: dict[str, Any] | None
) -> list[DistrictDelta]:
    before = (baseline or {}).get("allocation_by_district", {})
    after = (candidate or {}).get("allocation_by_district", {})
    names = sorted(set(before) | set(after))
    return [
        DistrictDelta(
            district=name,
            baseline=int(before.get(name, 0)),
            candidate=int(after.get(name, 0)),
            delta=int(after.get(name, 0)) - int(before.get(name, 0)),
        )
        for name in names
    ]


def _transfer_keys(allocation: dict[str, Any] | None) -> list[str]:
    return [
        f"{item['from_district']}->{item['to_district']}:{item['qty']}"
        for item in (allocation or {}).get("transfers", [])
    ]


def _changed_policy_keys(
    before: Mapping[str, Any], after: Mapping[str, Any]
) -> dict[str, dict[str, Any]]:
    keys = sorted(set(before) | set(after))
    return {
        key: {"baseline": before.get(key), "candidate": after.get(key)}
        for key in keys
        if before.get(key) != after.get(key)
    }


def _verdict(
    baseline: VisualSnapshot, candidate: VisualSnapshot, deltas: list[DistrictDelta]
) -> str:
    """一句话结论：只由两边的数字拼出来，不替策略下判断。"""
    parts: list[str] = [f"策略 {baseline.policy_version} → {candidate.policy_version}"]
    moved = [item for item in deltas if item.delta]
    if moved:
        parts.append(
            "分配变化 "
            + "，".join(f"{item.district} {item.baseline}→{item.candidate} 台" for item in moved)
        )
    else:
        parts.append("各片区分配未变")
    before_alloc = baseline.allocation or {}
    after_alloc = candidate.allocation or {}
    before_unmet = before_alloc.get("total_unmet")
    after_unmet = after_alloc.get("total_unmet")
    if before_unmet is not None and after_unmet is not None:
        parts.append(f"未满足合计 {before_unmet} → {after_unmet} 台")
    before_transfers = len(before_alloc.get("transfers", []))
    after_transfers = len(after_alloc.get("transfers", []))
    if before_transfers != after_transfers:
        parts.append(f"跨区调拨 {before_transfers} → {after_transfers} 次")
    return "；".join(parts)


def compare_snapshots(
    baseline: VisualSnapshot, candidate: VisualSnapshot
) -> CompareResult:
    deltas = _district_deltas(baseline.allocation, candidate.allocation)
    before_highlights = set(baseline.highlight_union)
    after_highlights = set(candidate.highlight_union)
    warnings = list(dict.fromkeys([*baseline.warnings, *candidate.warnings]))
    metrics_before = dict(baseline.metrics)
    metrics_after = dict(candidate.metrics)
    numeric_keys = sorted(
        key
        for key in set(metrics_before) | set(metrics_after)
        if isinstance(metrics_before.get(key), (int, float))
        and not isinstance(metrics_before.get(key), bool)
        and isinstance(metrics_after.get(key), (int, float))
        and not isinstance(metrics_after.get(key), bool)
    )
    return CompareResult(
        baseline_id=baseline.snapshot_id,
        candidate_id=candidate.snapshot_id,
        same_snapshot=baseline.snapshot_id == candidate.snapshot_id,
        policy={
            "baseline": dict(baseline.policy),
            "candidate": dict(candidate.policy),
            "changed": _changed_policy_keys(baseline.policy, candidate.policy),
        },
        allocation={
            "per_district": [item.model_dump(mode="json") for item in deltas],
            "transfers": {
                "baseline": _transfer_keys(baseline.allocation),
                "candidate": _transfer_keys(candidate.allocation),
            },
            "unmet": {
                "baseline": (baseline.allocation or {}).get("unmet_by_district", {}),
                "candidate": (candidate.allocation or {}).get("unmet_by_district", {}),
            },
            "total_allocated": {
                "baseline": (baseline.allocation or {}).get("total_allocated"),
                "candidate": (candidate.allocation or {}).get("total_allocated"),
            },
        },
        metrics={
            "baseline": metrics_before,
            "candidate": metrics_after,
            "delta": {
                key: metrics_after[key] - metrics_before[key] for key in numeric_keys
            },
        },
        scene={
            "scenario": {"baseline": baseline.scenario, "candidate": candidate.scenario},
            "scene_hash": {"baseline": baseline.scene_hash, "candidate": candidate.scene_hash},
            "highlights_added": sorted(after_highlights - before_highlights),
            "highlights_removed": sorted(before_highlights - after_highlights),
            "steps_changed": sum(
                1
                for before_step, after_step in zip(baseline.steps, candidate.steps)
                if before_step.highlights != after_step.highlights
            ),
        },
        verdict=_verdict(baseline, candidate, deltas),
        warnings=warnings,
    )


# --- 资产 -----------------------------------------------------------------


def asset_lookup(
    scenario: str,
    *,
    manifest_path: Path | str | None = None,
) -> dict[str, Any]:
    """核对 Blender 侧是否已有该场景的可导入资产。

    只核对、不生成：资产生成是离线动作（分钟级，要开 DCC），塞进推演循环会让一轮
    决策从秒级变成分钟级。缺失时返回可执行的离线命令。
    """
    scenario = str(scenario).strip()
    if scenario not in SCENARIOS:
        raise KeyError(f"未知场景: {scenario}")
    path = Path(manifest_path) if manifest_path else ASSET_MANIFEST
    if not path.is_file():
        raise FileNotFoundError(f"资产清单缺失: {path}")
    manifest = json.loads(path.read_text(encoding="utf-8"))
    entry = next(
        (item for item in manifest.get("scenarios", []) if item.get("scenario") == scenario),
        None,
    )
    if entry is None:
        raise KeyError(f"资产清单里没有场景: {scenario}")
    repo_root = path.resolve().parents[3]
    glb = repo_root / entry["glb"]
    blend = repo_root / entry["blend"]
    return {
        "scenario": scenario,
        "available": glb.is_file(),
        "glb": entry["glb"],
        "blend": entry["blend"],
        "glb_exists": glb.is_file(),
        "blend_exists": blend.is_file(),
        "objects": entry.get("objects", {}),
        "layout": manifest.get("layout", ""),
        "layout_snapshot": manifest.get("layout_snapshot", ""),
        "blender": manifest.get("blender", ""),
        "regenerate_command": (
            "uv run --locked python visualization/blender/generate_urban_twin.py "
            f"--scenario {scenario}"
        ),
        "note": "资产生成是离线动作；本工具只核对，不启动 Blender",
    }


# --- 工具调用日志 ---------------------------------------------------------


def append_tool_log(
    entry: Mapping[str, Any], root: Path | str | None = None
) -> Path:
    target = tool_log_path(root)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8") as handle:
        handle.write(
            json.dumps(dict(entry), ensure_ascii=False, sort_keys=True) + "\n"
        )
    return target


def read_tool_log(
    limit: int = 50, root: Path | str | None = None
) -> list[dict[str, Any]]:
    target = tool_log_path(root)
    if not target.is_file():
        return []
    lines = target.read_text(encoding="utf-8").splitlines()
    entries: list[dict[str, Any]] = []
    for line in lines[-max(0, int(limit)) :]:
        line = line.strip()
        if not line:
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return entries


__all__ = [
    "ACK_SCHEMA",
    "ASSET_MANIFEST",
    "COMPARE_SCHEMA",
    "CompareResult",
    "DEFAULT_AS_OF",
    "DEFAULT_EVENT_ID",
    "DEFAULT_LAYOUT_PATH",
    "DEFAULT_VISUAL_ROOT",
    "DistrictDelta",
    "JOB_SCHEMA",
    "RenderAction",
    "RenderJob",
    "RenderStep",
    "SCENARIOS",
    "VISUAL_SCHEMA",
    "VisualSnapshot",
    "ack_path",
    "acked",
    "append_tool_log",
    "asset_lookup",
    "build_render_job",
    "build_visual_snapshot",
    "compare_snapshots",
    "delivery_state",
    "job_path",
    "latest_snapshot_id",
    "load_job",
    "load_layout",
    "load_snapshot",
    "pick_scenario",
    "read_tool_log",
    "save_job",
    "save_snapshot",
    "snapshot_id_for",
    "snapshot_path",
    "tool_log_path",
    "visual_root",
    "write_ack",
    "read_ack",
]
