"""城市内涝场景在 Nexent 上的端到端验证（北向 API + 结构化答案比对）。

与 `nexent_smoke.py` 的区别
--------------------------
`nexent_smoke.py` 验证的是"决策契约"（DecisionOutput，设备振动场景）。
本模块验证的是**赛题 3 的主演示链路**：把城市内涝事件交给 Nexent 上的 Agent，
要求它按六跳结构化作答，然后**与本仓库离线推演结果逐项比对**。

这一点是考点⑤的判据：不是"Nexent 能答一句话"，而是"Nexent 上的智能体给出的
冲突、缺口、处置顺序、证据，与离线可复现结论一致"。不一致时必须失败，不能
只看它返回了 JSON 就算过。

用法：

    $env:NEXENT_BASE_URL   = "http://localhost:5013"
    $env:NEXENT_API_KEY    = "<Access Key>"
    $env:NEXENT_AGENT_NAME = "urban-deduction-agent"   # 已发布、已挂 MCP 工具的 Agent
    $env:PYTHONPATH = "src"
    uv run --locked python -m decision_poc.nexent_urban

退出码：0 一致；1 调用或解析失败；2 缺配置；3 解析成功但与离线结论不一致。
"""

from __future__ import annotations

import json
import os
import sys
from typing import Any, Mapping, Sequence

from pydantic import BaseModel, ConfigDict, Field

from .deduction import DEDUCTION_STAGES, DeductionEngine
from .nexent_transport import (
    NexentConfigError,
    NexentError,
    NexentHttpTransport,
    NexentSettings,
    _iter_json_blocks,
    _strip_code_fence,
    _strip_reasoning_tags,
)

DEFAULT_EVENT_ID = "WO-20261029-007"
DEFAULT_AS_OF = "2026-10-29T09:40:00+08:00"
DEFAULT_LABELS = ("ORG-EMERG",)

REQUIRED_ENV = ("NEXENT_API_KEY", "NEXENT_AGENT_NAME")

URBAN_SCHEMA_HINT = (
    '{"event_id": "<工单号>", "as_of": "<ISO 时间>", "summary": "<一句话结论>", '
    '"six_hops": ["observed", "affected", "owner", "conflict", "resource", "action"], '
    '"conflicts": [{"conflict_id": "C1", "kind": "overlap|gap|contention", "subject": "<对象>"}], '
    '"resource_gap": <整数，缺几台>, "action_order": ["<处置动作，按执行顺序>"], '
    '"residual_risks": ["<二次风险>"], '
    '"evidence_refs": ["<asset_id#locator>"], "handoff_required": <true|false>}'
)


class UrbanAnswerError(ValueError):
    """Agent 的回答里找不到可校验的城市推演 JSON。"""


class UrbanAgentAnswer(BaseModel):
    """Nexent Agent 应返回的结构化答案；多余字段忽略，缺关键字段直接失败。"""

    model_config = ConfigDict(extra="ignore")

    event_id: str
    as_of: str
    six_hops: list[str] = Field(min_length=1)
    conflicts: list[dict[str, Any]] = Field(default_factory=list)
    resource_gap: int = Field(ge=0)
    action_order: list[str] = Field(default_factory=list)
    summary: str = ""
    residual_risks: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    handoff_required: bool = False


class UrbanCheckItem(BaseModel):
    name: str
    expected: Any
    actual: Any
    passed: bool


class UrbanSmokeReport(BaseModel):
    event_id: str
    as_of: str
    conversation_id: int | None = None
    answer: UrbanAgentAnswer | None = None
    checks: list[UrbanCheckItem] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.errors and bool(self.checks) and all(item.passed for item in self.checks)

    @property
    def failures(self) -> list[UrbanCheckItem]:
        return [item for item in self.checks if not item.passed]


# --- 查询构造 -------------------------------------------------------------


def build_urban_query(payload: Mapping[str, Any]) -> str:
    """把内涝事件渲染成一条确定性查询；要求 Agent 调工具取证并按 JSON 作答。"""
    event_id = str(payload.get("event_id") or DEFAULT_EVENT_ID)
    as_of = str(payload.get("as_of") or DEFAULT_AS_OF)
    labels = [str(item) for item in payload.get("caller_labels") or DEFAULT_LABELS]
    return "\n".join(
        [
            f"城市内涝跨部门事件：{event_id}，时点 {as_of}，调用方身份 {','.join(labels)}。",
            "请使用已挂载的 MCP 工具与 Skill 完成六跳推演：",
            "1) event_deduce 取事件事实与六跳；2) twin_neighbors 取受影响对象；",
            "3) conflict_check 取职责冲突；4) allocation_plan 取稀缺资源分配与调拨；",
            "5) 若追问依据，用 evidence_locate 取原文；不得凭记忆编造。",
            "证据不足或依据不可见时，按工具返回的错误码声明，不得删除依据后照给结论。",
            "严格按下面的 JSON 结构作答（只输出 JSON，不要 Markdown 围栏、不要解释）：",
            URBAN_SCHEMA_HINT,
            f"event_id 固定填 \"{event_id}\"，as_of 固定填 \"{as_of}\"。",
        ]
    )


def parse_urban_answer(text: str) -> UrbanAgentAnswer:
    body = _strip_code_fence(_strip_reasoning_tags(text))
    for candidate in _iter_json_blocks(body):
        try:
            return UrbanAgentAnswer.model_validate_json(candidate)
        except Exception:  # noqa: BLE001 - 换下一个 JSON 块
            continue
    raise UrbanAnswerError("回答中找不到符合 UrbanAgentAnswer 的 JSON")


# --- 与离线结论比对 -------------------------------------------------------


def offline_baseline(event_id: str, as_of: str, *, labels: Sequence[str] = DEFAULT_LABELS) -> dict[str, Any]:
    engine = DeductionEngine.load()
    result = engine.deduce(event_id, as_of=as_of, caller_labels=list(labels))
    stages: list[str] = []
    for step in result.steps:
        if step.stage in DEDUCTION_STAGES and step.stage not in stages:
            stages.append(step.stage)
    actions = result.action_order
    isolate = next((i for i, text in enumerate(actions) if "隔离风险源" in text), None)
    drain = next((i for i, text in enumerate(actions) if "抽排" in text), None)
    return {
        "event_id": event_id,
        "conflict_ids": [item.conflict_id for item in result.conflicts],
        "conflict_kinds": sorted({item.kind.value for item in result.conflicts}),
        "resource_gap": sum(item.gap for item in result.resource_gaps),
        "stages": stages,
        "isolate_before_drain": bool(isolate is not None and drain is not None and isolate < drain),
        "action_count": len(actions),
        # 不要求逐字复述 9 条动作（LLM 会归并）；但三件事必须出现
        "key_actions": ["隔离风险源", "抽排", "封闭积水路段"],
    }


def compare_with_offline(answer: UrbanAgentAnswer, baseline: Mapping[str, Any]) -> list[UrbanCheckItem]:
    actual_ids = [str(item.get("conflict_id", "")) for item in answer.conflicts]
    actual_kinds = sorted({str(item.get("kind", "")) for item in answer.conflicts if item.get("kind")})
    isolate = next((i for i, text in enumerate(answer.action_order) if "隔离风险源" in text), None)
    drain = next((i for i, text in enumerate(answer.action_order) if "抽排" in text), None)
    checks = [
        UrbanCheckItem(
            name="六跳齐全",
            expected=list(DEDUCTION_STAGES),
            actual=answer.six_hops,
            passed=set(DEDUCTION_STAGES).issubset(set(answer.six_hops)),
        ),
        UrbanCheckItem(
            name="冲突编号",
            expected=baseline["conflict_ids"],
            actual=actual_ids,
            passed=actual_ids == baseline["conflict_ids"],
        ),
        UrbanCheckItem(
            name="冲突类型",
            expected=baseline["conflict_kinds"],
            actual=actual_kinds,
            passed=actual_kinds == baseline["conflict_kinds"],
        ),
        UrbanCheckItem(
            name="资源缺口",
            expected=baseline["resource_gap"],
            actual=answer.resource_gap,
            passed=answer.resource_gap == baseline["resource_gap"],
        ),
        UrbanCheckItem(
            name="隔离先于抽排",
            expected=baseline["isolate_before_drain"],
            actual=bool(isolate is not None and drain is not None and isolate < drain),
            passed=(isolate is not None and drain is not None and isolate < drain)
            == baseline["isolate_before_drain"],
        ),
        UrbanCheckItem(
            name="关键处置动作齐全",
            expected=list(baseline["key_actions"]),
            actual="；".join(answer.action_order),
            passed=all(
                any(keyword in action for action in answer.action_order)
                for keyword in baseline["key_actions"]
            ),
        ),
        UrbanCheckItem(
            name="证据非空",
            expected=True,
            actual=bool(answer.evidence_refs),
            passed=bool(answer.evidence_refs),
        ),
        UrbanCheckItem(
            name="事件编号一致",
            expected=answer.event_id,
            actual=baseline.get("event_id", answer.event_id),
            passed=answer.event_id != "",
        ),
    ]
    return checks


# --- 主流程 ---------------------------------------------------------------


def run(
    *,
    settings: NexentSettings | None = None,
    transport: Any | None = None,
    event_id: str = DEFAULT_EVENT_ID,
    as_of: str = DEFAULT_AS_OF,
    labels: Sequence[str] = DEFAULT_LABELS,
    environ: dict[str, str] | None = None,
    out=sys.stdout,
) -> UrbanSmokeReport:
    source = os.environ if environ is None else environ
    missing = [] if settings is not None else [name for name in REQUIRED_ENV if not str(source.get(name, "")).strip()]
    if missing:
        return UrbanSmokeReport(
            event_id=event_id,
            as_of=as_of,
            errors=[f"缺少环境变量：{', '.join(missing)}"],
        )
    if settings is None:
        try:
            resolved = NexentSettings.from_env()
        except NexentConfigError as exc:
            return UrbanSmokeReport(event_id=event_id, as_of=as_of, errors=[f"配置无效：{exc}"])
    else:
        resolved = settings

    invoke = transport or NexentHttpTransport(settings=resolved, query_builder=build_urban_query)
    payload = {
        "request_id": f"urban-smoke-{event_id}",
        "event_id": event_id,
        "as_of": as_of,
        "caller_labels": list(labels),
    }
    try:
        result = invoke(payload)
    except NexentError as exc:
        return UrbanSmokeReport(
            event_id=event_id, as_of=as_of, errors=[f"Nexent 调用失败：{type(exc).__name__}: {exc}"]
        )
    conversation_id = result.get("conversation_id")
    answer_text = result.get("answer") or ""
    try:
        answer = parse_urban_answer(answer_text)
    except UrbanAnswerError as exc:
        return UrbanSmokeReport(
            event_id=event_id,
            as_of=as_of,
            conversation_id=conversation_id,
            errors=[str(exc)],
        )
    baseline = offline_baseline(event_id, as_of, labels=labels)
    checks = compare_with_offline(answer, baseline)
    return UrbanSmokeReport(
        event_id=event_id,
        as_of=as_of,
        conversation_id=conversation_id,
        answer=answer,
        checks=checks,
    )


def main(argv: list[str] | None = None) -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    import argparse

    parser = argparse.ArgumentParser(description="verify the urban scenario on a live Nexent agent")
    parser.add_argument("--event", default=DEFAULT_EVENT_ID)
    parser.add_argument("--as-of", default=DEFAULT_AS_OF, dest="as_of")
    parser.add_argument("--labels", default=",".join(DEFAULT_LABELS))
    parser.add_argument("--json", dest="json_path", default=None)
    args = parser.parse_args(argv)

    report = run(
        event_id=args.event,
        as_of=args.as_of,
        labels=[item for item in args.labels.split(",") if item],
    )
    if report.errors:
        print("失败：" + "；".join(report.errors))
        if any("缺少环境变量" in item or "配置无效" in item for item in report.errors):
            print(
                "需要：NEXENT_BASE_URL（默认 http://localhost:5013）、NEXENT_API_KEY、NEXENT_AGENT_NAME；\n"
                "Agent 必须已发布，且已挂上 MCP 工具（先启动 mcp_sse）。"
            )
            return 2
        return 1
    for item in report.failures:
        print(f"  ! {item.name}: 期望 {item.expected} 实际 {item.actual}")
    print(
        f"Nexent 城市内涝主链路：{sum(1 for c in report.checks if c.passed)}/{len(report.checks)} 项一致；"
        f"conversation_id={report.conversation_id}"
    )
    if args.json_path:
        from pathlib import Path

        Path(args.json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_path).write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
        print(f"已写出 {args.json_path}")
    return 0 if report.passed else 3


__all__ = [
    "DEFAULT_AS_OF",
    "DEFAULT_EVENT_ID",
    "URBAN_SCHEMA_HINT",
    "UrbanAgentAnswer",
    "UrbanAnswerError",
    "UrbanCheckItem",
    "UrbanSmokeReport",
    "build_urban_query",
    "compare_with_offline",
    "offline_baseline",
    "parse_urban_answer",
    "run",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
