"""城市内涝反馈的领域契约，以及到通用 `FeedbackRecord` 的适配。

**为什么要有这一层**：`contracts.FeedbackRecord` 只有
`request_id / outcome / note / proposed_updates` 四个通用字段，装不下"哪一片区、多深、
哪几条路被淹、泵站在什么状态"。直接把内涝数据塞进 `proposed_updates` 会让下游分不清
"这是观测"还是"这是参数建议"，两者混在一起就再也无法审计。

**边界（重要）**：本模块**只描述观测与偏差，不产出策略参数**。
`proposed_updates` 里放的是证据与偏差方向，参数改动一律交给
`policy_evolution.candidate_patches()` 生成、由 12 例冻结门禁判定是否采纳。
理由：反馈只能决定"往哪个方向试"，不能直接改 ground truth 上的最优解——
否则冻结的 12 例口径会随每条反馈漂移，评测立刻失效。

**合规**：`source` 复用 `ProvenanceLevel`，SIM 输出一律记 `synthetic`，
绝不因为"跑的是正经水文模型"就标成 `real`。`note` 里强制带上来源标记，
防止人工汇报时把合成数据说成实测。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Literal, Sequence

from pydantic import Field, field_validator

from .contracts import (
    FeedbackOutcome,
    FeedbackRecord,
    ProvenanceLevel,
    StrictModel,
    _nonblank,
)

#: 各片区内涝判定临界水深（cm）。A-03 取台账峰值 30cm，B-01 取 18cm。
#: 缺省 30 是保守值：宁可把"刚好没淹"判成不达标，也不要把真淹了判成达标。
#:
#: 这两个数值是**这座城市的标定**，不该写死在代码里。换城市时把
#: `<district_id>,critical_depth_cm` 放进数据集根目录的 district_thresholds.csv（可选），
#: 这里只留兜底；文件不存在时行为与以前逐位一致，所以现有样本不受影响。
_BUILTIN_CRITICAL_CM: dict[str, float] = {"A-03": 30.0, "B-01": 18.0}
DEFAULT_CRITICAL_CM = 30.0
THRESHOLD_FILE = "district_thresholds.csv"


def _load_critical_depth() -> dict[str, float]:
    """片区阈值表：优先读数据集里的 district_thresholds.csv，缺省用内置表。"""
    from .urban_dataset import DEFAULT_DATASET_ROOT

    path = DEFAULT_DATASET_ROOT / THRESHOLD_FILE
    table = dict(_BUILTIN_CRITICAL_CM)
    if not path.is_file():
        return table
    import csv

    with path.open(encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            district = (row.get("district_id") or "").strip()
            raw = (row.get("critical_depth_cm") or "").strip()
            if not district or not raw:
                continue
            try:
                table[district] = float(raw)
            except ValueError:
                continue
    return table


CRITICAL_DEPTH_CM: dict[str, float] = _load_critical_depth()

#: 显著回落阈值：低于临界值的 40% 才算"处置有效"，中间地带一律 neutral。
#: 设一档"说不清"比强行二分类诚实——19cm 到底算不算解决了，取决于片区和时点。
POSITIVE_RATIO = 0.40

PumpState = Literal["idle", "running", "fault", "unknown"]

#: 运行期记忆落盘位置。**刻意不指向 `samples/eval/decision_memory.json`**：
#: 那是冻结资产（被 `test_frozen_numbers` 与 12 例口径共同守着），
#: 运行时写入会让"同一份样本跑两次结果不同"。放 artifacts/ 下，已被 .gitignore 排除。
def default_memory_path() -> Path:
    return Path(__file__).resolve().parents[2] / "artifacts" / "memory" / "decision_memory.json"


class FloodFeedback(StrictModel):
    """一次内涝推演/观测的反馈记录（人工注入，不由 LLM 解释）。"""

    scenario_id: str = Field(min_length=1)
    t_min: int = Field(ge=0, description="事件起始后的分钟数")
    district: str = Field(min_length=1)
    water_depth_cm: float = Field(ge=0.0)
    flooded_road_ids: list[str] = Field(default_factory=list)
    pump_state: PumpState = "unknown"
    source: ProvenanceLevel = ProvenanceLevel.SYNTHETIC
    seed: int | None = Field(default=None, description="模拟种子，用于复现")
    expected_depth_cm: float | None = Field(
        default=None,
        description="推演给出的预期水深；为空表示没有可比对的预测，只按临界值判定",
    )
    note: str = ""

    @field_validator("scenario_id", "district")
    @classmethod
    def _reject_blank(cls, value: str) -> str:
        return _nonblank(value)

    @field_validator("flooded_road_ids")
    @classmethod
    def _dedupe_roads(cls, value: list[str]) -> list[str]:
        # 去重但保序：顺序本身是"优先处置哪条路"的弱信号，不要用 set 打乱
        seen: set[str] = set()
        out: list[str] = []
        for item in value:
            road = _nonblank(item)
            if road not in seen:
                seen.add(road)
                out.append(road)
        return out


def critical_depth(district: str) -> float:
    """该片区的临界水深；未知片区退回保守缺省值。"""
    return CRITICAL_DEPTH_CM.get(district, DEFAULT_CRITICAL_CM)


def classify(feedback: FloodFeedback) -> FeedbackOutcome:
    """按"处置后是否仍内涝"判定效果，而不是按预测准不准。

    预测精度是另一件事（由 `expected_depth_cm` 的偏差单独记录在证据里），
    混进 outcome 会让"模型预测很准但处置无效"被记成 positive。
    """
    critical = critical_depth(feedback.district)
    if feedback.water_depth_cm >= critical:
        return FeedbackOutcome.NEGATIVE
    if feedback.water_depth_cm <= critical * POSITIVE_RATIO:
        return FeedbackOutcome.POSITIVE
    return FeedbackOutcome.NEUTRAL


def feedback_id(feedback: FloodFeedback) -> str:
    """确定性 ID：同一份反馈逐字节复现同一个 ID，便于去重与回放。

    不用 uuid4——那会让"同一输入跑两次得到不同 id"，回放与去重全部失效。
    """
    payload = feedback.model_dump(mode="json", exclude={"note"})
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:12]
    return f"FB-{digest}"


def evidence(feedback: FloodFeedback) -> dict[str, Any]:
    """把观测摊平成可审计的证据块（只放观测，不放参数建议）。"""
    critical = critical_depth(feedback.district)
    block: dict[str, Any] = {
        "scenario_id": feedback.scenario_id,
        "district": feedback.district,
        "t_min": feedback.t_min,
        "water_depth_cm": feedback.water_depth_cm,
        "critical_cm": critical,
        "over_by_cm": round(feedback.water_depth_cm - critical, 3),
        "flooded_road_count": len(feedback.flooded_road_ids),
        "pump_state": feedback.pump_state,
        "source": str(feedback.source),
        "seed": feedback.seed,
    }
    if feedback.expected_depth_cm is not None:
        block["expected_depth_cm"] = feedback.expected_depth_cm
        block["prediction_error_cm"] = round(
            feedback.water_depth_cm - feedback.expected_depth_cm, 3
        )
    return block


def to_feedback_record(feedback: FloodFeedback) -> FeedbackRecord:
    """领域反馈 → 通用 FeedbackRecord。

    `proposed_updates` 里刻意**不含任何参数名**：只有证据块和偏差方向。
    下游要改参数，必须走 `policy_evolution.candidate_patches()` + 12 例门禁。
    """
    outcome = classify(feedback)
    block = evidence(feedback)
    over = block["over_by_cm"]
    direction = "raise_capacity" if over > 0 else "hold"
    note_parts = [
        f"[{feedback.source}] {feedback.district} t={feedback.t_min}min "
        f"水深 {feedback.water_depth_cm}cm（临界 {block['critical_cm']}cm）"
    ]
    if feedback.flooded_road_ids:
        note_parts.append("受淹道路 " + ",".join(feedback.flooded_road_ids))
    if feedback.expected_depth_cm is not None:
        note_parts.append(f"相对预期偏差 {block['prediction_error_cm']:+.1f}cm")
    if feedback.source is not ProvenanceLevel.REAL:
        note_parts.append("【非实测数据，不得作为真实业绩引用】")
    if feedback.note:
        note_parts.append(feedback.note)
    return FeedbackRecord(
        request_id=feedback_id(feedback),
        outcome=outcome,
        note="；".join(note_parts),
        proposed_updates={
            "direction": direction,
            "evidence": block,
        },
    )


def suggest_dimension(feedback: FloodFeedback) -> str | None:
    """把一次反馈映射成 `policy_evolution.candidate_patches()` 的维度名。

    **只在"没压住"时提方向**，其余一律返回 None（不动策略）。三条依据：

    1. 泵站 idle/fault 且仍超临界 → 这是执行问题，不是策略问题。调权重不会让泵转起来，
       反而会在 12 例上伪造出"变好了"的假象 → 返回 None，交人工处置。
    2. 超临界且受淹点 ≥2 → `critical_unmet`：多点同时失守，说明"危险源"这个信号没进关键集，
       调拨门槛再收紧也救不回来。对应 candidate_patches 里补 `weight_hazard` 的分支。
    3. 超临界且只淹 1 点 → `transfer`：单点失守更像"该调没调过来"，
       走封路优先序 / 调拨门槛分支。

    刻意**不**因为"预测偏差"而调参：预测偏浅只说明模型保守，不构成改策略的理由；
    拿它调参会让 12 例上的分数好看，实盘却没依据。
    """
    over = feedback.water_depth_cm - critical_depth(feedback.district)
    if over <= 0:
        return None
    if feedback.pump_state in ("idle", "fault"):
        return None
    if len(feedback.flooded_road_ids) >= 2:
        return "critical_unmet"
    if len(feedback.flooded_road_ids) == 1:
        return "transfer"
    return None


def plan(feedback: FloodFeedback, policy: Any | None = None) -> dict[str, Any]:
    """给出一条可审计的"建议动作"，不执行、不落盘。

    `policy` 为 None 时只给维度名与理由；传了策略对象才把候选补丁一并算出来。
    """
    dimension = suggest_dimension(feedback)
    out: dict[str, Any] = {
        "feedback_id": feedback_id(feedback),
        "outcome": str(classify(feedback)),
        "dimension": dimension,
        "reason": _dimension_reason(feedback, dimension),
    }
    if dimension and policy is not None:
        from .policy_evolution import candidate_patches

        out["candidates"] = candidate_patches(dimension, policy)
    return out


def _dimension_reason(feedback: FloodFeedback, dimension: str | None) -> str:
    over = feedback.water_depth_cm - critical_depth(feedback.district)
    if over <= 0:
        return "未超临界，维持当前策略"
    if dimension is None:
        return "超临界但泵站未运行，属执行问题，不由策略调整解决"
    if dimension == "critical_unmet":
        return f"超临界 {over:.1f}cm 且 {len(feedback.flooded_road_ids)} 点受淹，疑危险源未入关键集"
    return f"超临界 {over:.1f}cm 且单点受淹，疑调拨门槛过紧"


def run_evolution(
    feedback: FloodFeedback,
    *,
    max_rounds: int = 2,
    memory_path: "Path | str | None" = None,
    use_memory: bool = True,
    persist_memory: bool = True,
    reviews: "Sequence[Any] | None" = None,
) -> dict[str, Any]:
    """反馈触发一轮策略演化。

    **只读 12 例、不写 samples/**：`evolve()` 本身无落盘副作用，`VersionRegistry` 纯内存。
    唯一的写入是运行期记忆（`artifacts/memory/decision_memory.json`），
    与 `samples/eval/decision_memory.json` 这个冻结资产完全分开。

    `use_memory=True` 时走 warm start：若记忆里有同场景已验证有效的策略，
    直接从它起步而不是从 v1.0 重跑。效果有两面，都要如实说：

    - 已收敛（记忆里是 v1.2、训练偏差 0）→ 不再发新版本，标签**保持不变**；
    - 未收敛（记忆里是 v1.1）→ 从 v1.1 的终点继续，新版本号**接着往下排**（v1.2），
      不会退回 v1.1。这条靠 `policy_evolution.label_minor()` 保证。

    `reviews` 是**第二进化信号**（专家评审），默认读 `samples/eval/decision_reviews.json`。
    它在这条链上的作用要说清，别夸大：**评审只能否决，不能驱动**。
    因为门禁要求候选在评审用例上的偏差为 0（与专家给的修正分配完全一致），
    评审偏差 > 0 直接把整轮拒掉；而当训练偏差已经为 0 时，评审无法让偏差再下降。
    所以评审的价值是"防止策略在训练集上过拟合、且能被专家一票否决"，
    不是"靠评审继续往上走版本号"。

    这里刻意不把 `suggest_dimension()` 的结论塞给 `evolve()`：
    `evolve()` 的维度由 `worst_dimension(active_train)` 从训练集偏差里选，
    外部指定维度会让"为什么改这个参数"变成人为挑的，门禁就失去意义。

    反馈在这条链里承担两件事：

    1. **触发**：没有反馈就不跑演化（`dimension is None` 时直接返回 triggered=False）；
    2. **证据**：反馈本身作为可审计输入留存，能说清"这次调整是被哪条现场记录催出来的"。

    是否采纳仍由 12 例（8 train + 4 holdout）的偏差判定，与反馈无关——这一层分离是
    "可进化"能被评委复核的前提。
    """
    dimension = suggest_dimension(feedback)
    base: dict[str, Any] = {
        "triggered": False,
        "dimension_hint": dimension,
        "reason": _dimension_reason(feedback, dimension),
    }
    if dimension is None:
        return base

    from .experience import DecisionMemory
    from .policy_evolution import (
        entries_from_report,
        evolve,
        load_cases,
        load_reviews,
        signature_for,
    )

    path = Path(memory_path) if memory_path is not None else default_memory_path()
    cases = load_cases()
    # 评审默认取仓库里的既有样本（`samples/eval/decision_reviews.json`），**只读不写**。
    # 只有 `modify` 且给了修正分配的评审才会变成门禁用例，`reject` 只留痕不编期望。
    active_reviews = list(reviews) if reviews is not None else load_reviews()
    memory = DecisionMemory.load(path) if use_memory else DecisionMemory()
    warm = memory.warm_start(signature_for(list(cases))) if use_memory else None
    entries_before = len(memory)

    report = evolve(
        cases=cases,
        max_rounds=max_rounds,
        include_real_event=False,
        memory=memory if use_memory else None,
        reviews=active_reviews,
    )

    # 沉淀：把这一轮已验证的结果写成经验。`remember` 按 entry_id 幂等，
    # 同一条反馈重复提交不会把记忆灌成几十条重复项。
    if persist_memory and use_memory:
        memory.remember_many(entries_from_report(report, cases, active_reviews))
        memory.save(path)

    base.update(
        {
            "triggered": True,
            "final_label": report.final_label,
            "reviews": {
                "count": len(active_reviews),
                "gate_cases": report.review_cases,
                "items": [item.model_dump(mode="json") for item in report.reviews],
            },
            "memory": {
                "enabled": use_memory,
                "hit": warm is not None,
                "hit_label": warm.entry.label if warm is not None else None,
                "hit_deviation": (
                    warm.entry.outcome.get("deviation") if warm is not None else None
                ),
                "entries_before": entries_before,
                "entries_after": len(memory),
                "path": str(path) if persist_memory and use_memory else "",
            },
            "train": {
                "initial": report.initial_train.deviation,
                "final": report.final_train.deviation,
                "drop": report.train_deviation_drop(),
            },
            "holdout": {
                "initial": report.initial_holdout.deviation,
                "final": report.final_holdout.deviation,
                "drop": report.holdout_deviation_drop(),
            },
            "promoted_rounds": report.promoted_rounds,
            "rejected_rounds": report.rejected_rounds,
            "rounds": [
                {
                    "round": item.round,
                    "target_dimension": item.target_dimension,
                    "delta": item.params_delta,
                    "train_before": item.train_before,
                    "train_after": item.train_after,
                    "action": item.action,
                    "reason": item.reason,
                }
                for item in report.rounds
            ],
        }
    )
    return base


def main(argv: list[str] | None = None) -> int:
    """最小可用入口：把一份反馈 JSON 转成 FeedbackRecord 并打印。

    只做转换与展示，不写库、不触发门禁——注入动作必须显式发生，
    避免"跑一下脚本就把策略改了"这种不可审计的副作用。
    """
    import sys

    args = list(sys.argv[1:] if argv is None else argv)
    show_plan = "--plan" in args
    show_evolve = "--evolve" in args
    args = [a for a in args if not a.startswith("--")]
    if not args:
        print(
            "usage: python -m decision_poc.flood_feedback <feedback.json> [--plan] [--evolve]",
            file=sys.stderr,
        )
        return 2
    raw = json.loads(open(args[0], encoding="utf-8").read())
    records = raw if isinstance(raw, list) else [raw]
    for item in records:
        fb = FloodFeedback.model_validate(item)
        rec = to_feedback_record(fb)
        payload: dict[str, Any] = rec.model_dump(mode="json")
        if show_plan:
            payload["plan"] = plan(fb)
        if show_evolve:
            payload["evolution"] = run_evolution(fb)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
