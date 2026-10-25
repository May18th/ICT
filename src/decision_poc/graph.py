import asyncio
from typing import Any, Literal

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from .agent import generate_decision_async
from .contracts import (
    ApprovalDecision,
    ApprovalStatus,
    DecisionOutput,
    DecisionPolicy,
    DecisionRequest,
    EvaluationReport,
    FeedbackRecord,
    ToolContext,
    ToolError,
    ToolResult,
    ToolStats,
)
from .evolution import VersionRegistry
from .tools import SimulatedActionTool, SimulatedEvidenceTool


class DecisionState(TypedDict, total=False):
    request: dict[str, Any]
    model_output: dict[str, Any]
    evidence_results: list[dict[str, Any]]
    evidence: list[dict[str, Any]]
    evidence_result: dict[str, Any]
    evidence_status: str
    decision: dict[str, Any]
    approval: dict[str, Any] | None
    execution: dict[str, Any] | None
    feedback: dict[str, Any] | None
    evaluation_report: dict[str, Any] | None
    candidate: dict[str, Any] | None
    active_version: dict[str, Any] | None
    version_history: list[dict[str, Any]]
    evolution_status: str
    trace: list[str]
    termination_reason: str
    retry_counts: dict[str, int]


def _tool_from_state(value: dict[str, Any]) -> ToolResult:
    return ToolResult.model_validate(value)


def _default_evidence(request: DecisionRequest) -> ToolResult:
    return ToolResult(
        status="ok",
        data={
            "items": [
                {
                    "source": "mock://context",
                    "content": f"Context keys: {sorted(request.context)}",
                }
            ]
        },
        error=None,
        stats=ToolStats(attempt=1, latency_ms=0),
        context=ToolContext(source="mock://context", version="v1"),
    )


def _collect_evidence_node(
    state: DecisionState, policy: DecisionPolicy
) -> dict[str, Any]:
    request = DecisionRequest.model_validate(state["request"])
    scripted = state.get("evidence_results") or []
    result = SimulatedEvidenceTool(max_attempts=policy.max_tool_attempts).run(
        request_id=request.request_id,
        scripted_results=[_tool_from_state(item) for item in scripted],
    )
    if result.status == "error":
        return {
            "evidence_status": "error",
            "evidence": [],
            "evidence_result": result.model_dump(mode="json"),
            "retry_counts": {"evidence": result.stats.attempt},
            "termination_reason": "evidence_failed",
            "trace": [*state.get("trace", []), "evidence_failed"],
        }

    evidence = result.data.get("items", [])
    return {
        "evidence_status": "ok",
        "evidence": evidence,
        "evidence_result": result.model_dump(mode="json"),
        "retry_counts": {
            "evidence": result.stats.attempt
        },
        "trace": [*state.get("trace", []), "evidence_collected"],
    }


async def _generate_node(state: DecisionState) -> dict[str, Any]:
    request = DecisionRequest.model_validate(state["request"])
    decision = await generate_decision_async(
        request,
        state.get("evidence", []),
        state["model_output"],
    )
    return {
        "decision": decision.model_dump(mode="json"),
        "trace": [*state.get("trace", []), "decision_generated"],
    }


def _validate_node(state: DecisionState) -> dict[str, Any]:
    request = DecisionRequest.model_validate(state["request"])
    decision = DecisionOutput.model_validate(state["decision"])
    if decision.request_id != request.request_id:
        raise ValueError(
            "request_id mismatch: "
            f"expected {request.request_id}, got {decision.request_id}"
        )
    if not state.get("evidence"):
        raise ValueError("decision requires collected evidence")
    available_evidence = {
        (
            item.get("source"),
            item.get("version"),
            item.get("content"),
        )
        for item in state["evidence"]
    }
    if any(
        (item.source, item.version, item.content) not in available_evidence
        for item in decision.evidence
    ):
        raise ValueError("evidence not found in collected tool results")
    return {"trace": [*state.get("trace", []), "decision_validated"]}


def _approval_node(state: DecisionState, policy: DecisionPolicy) -> dict[str, Any]:
    decision = DecisionOutput.model_validate(state["decision"])
    supplied = state.get("approval")
    if supplied not in {None, "approved", "rejected"}:
        raise ValueError("approval must be approved, rejected, or omitted")
    if decision.risk_level in policy.approval_risk_levels and supplied is None:
        approval = ApprovalDecision(
            status=ApprovalStatus.REQUIRED,
            reason="risk level requires explicit simulated approval",
        )
        return {
            "approval": approval.model_dump(mode="json"),
            "termination_reason": "approval_required",
            "trace": [*state.get("trace", []), "approval_required"],
        }

    if supplied == "rejected":
        approval = ApprovalDecision(
            status=ApprovalStatus.REJECTED,
            reason="simulated approval was rejected",
        )
        return {
            "approval": approval.model_dump(mode="json"),
            "termination_reason": "approval_rejected",
            "trace": [*state.get("trace", []), "approval_rejected"],
        }

    approval = ApprovalDecision(
        status=ApprovalStatus.APPROVED,
        reason="simulated approval accepted or not required",
    )
    return {
        "approval": approval.model_dump(mode="json"),
        "trace": [*state.get("trace", []), "approval_accepted"],
    }


def _execute_node(state: DecisionState) -> dict[str, Any]:
    request = DecisionRequest.model_validate(state["request"])
    decision = DecisionOutput.model_validate(state["decision"])
    execution = SimulatedActionTool().run(
        request_id=request.request_id,
        option_id=decision.selected_option_id,
    )
    return {
        "execution": execution.model_dump(mode="json"),
        "trace": [*state.get("trace", []), "execution_completed"],
    }


def _feedback_node(state: DecisionState) -> dict[str, Any]:
    feedback = FeedbackRecord.model_validate(state["feedback"])
    request = DecisionRequest.model_validate(state["request"])
    if feedback.request_id != request.request_id:
        raise ValueError("feedback request_id mismatch")
    return {
        "feedback": feedback.model_dump(mode="json"),
        "trace": [*state.get("trace", []), "feedback_recorded"],
    }


def _evolve_node(state: DecisionState, registry: VersionRegistry) -> dict[str, Any]:
    feedback = FeedbackRecord.model_validate(state["feedback"])
    candidate = registry.propose(feedback)
    raw_report = state.get("evaluation_report")
    if raw_report is None:
        return {
            "candidate": candidate.model_dump(mode="json"),
            "active_version": registry.active.model_dump(mode="json"),
            "version_history": [item.model_dump(mode="json") for item in registry.history],
            "evolution_status": "awaiting_evaluation",
            "trace": [*state.get("trace", []), "candidate_awaiting_evaluation"],
        }

    report = EvaluationReport.model_validate(raw_report)
    active = registry.promote(candidate, report)
    promoted = active.version_id == candidate.version_id
    return {
        "candidate": candidate.model_dump(mode="json"),
        "active_version": active.model_dump(mode="json"),
        "version_history": [item.model_dump(mode="json") for item in registry.history],
        "evolution_status": "promoted" if promoted else "rejected",
        "trace": [
            *state.get("trace", []),
            "candidate_promoted" if promoted else "candidate_rejected",
        ],
    }


def _finish_node(state: DecisionState) -> dict[str, Any]:
    return {
        "termination_reason": state.get("termination_reason", "completed"),
        "trace": state.get("trace", []),
    }


def _route_evidence(state: DecisionState) -> Literal["generate", "finish"]:
    return "generate" if state.get("evidence_status") == "ok" else "finish"


def _route_approval(state: DecisionState) -> Literal["execute", "finish"]:
    status = state.get("approval", {}).get("status")
    return "execute" if status == ApprovalStatus.APPROVED.value else "finish"


def _route_feedback(state: DecisionState) -> Literal["feedback", "finish"]:
    return "feedback" if state.get("feedback") is not None else "finish"


def _route_evolution(state: DecisionState) -> Literal["evolve", "finish"]:
    return "evolve"


def build_graph(
    *,
    policy: DecisionPolicy | None = None,
    registry: VersionRegistry | None = None,
):
    policy = policy or DecisionPolicy()
    registry = registry or VersionRegistry(initial_parameters=policy.initial_parameters)
    graph = StateGraph(DecisionState)
    graph.add_node("collect_evidence", lambda state: _collect_evidence_node(state, policy))
    graph.add_node("generate_decision", _generate_node)
    graph.add_node("validate_decision", _validate_node)
    graph.add_node("check_approval", lambda state: _approval_node(state, policy))
    graph.add_node("execute", _execute_node)
    graph.add_node("record_feedback", _feedback_node)
    graph.add_node("evolve", lambda state: _evolve_node(state, registry))
    graph.add_node("finish", _finish_node)
    graph.add_edge(START, "collect_evidence")
    graph.add_conditional_edges(
        "collect_evidence", _route_evidence, {"generate": "generate_decision", "finish": "finish"}
    )
    graph.add_edge("generate_decision", "validate_decision")
    graph.add_edge("validate_decision", "check_approval")
    graph.add_conditional_edges(
        "check_approval", _route_approval, {"execute": "execute", "finish": "finish"}
    )
    graph.add_conditional_edges(
        "execute", _route_feedback, {"feedback": "record_feedback", "finish": "finish"}
    )
    graph.add_conditional_edges(
        "record_feedback", _route_evolution, {"evolve": "evolve", "finish": "finish"}
    )
    graph.add_edge("evolve", "finish")
    graph.add_edge("finish", END)
    return graph.compile()


def run_decision(
    request: DecisionRequest,
    *,
    model_output: dict[str, Any],
    evidence_results: list[ToolResult] | None = None,
    approval: str | None = None,
    feedback: FeedbackRecord | None = None,
    evaluation_report: EvaluationReport | dict[str, Any] | None = None,
    policy: DecisionPolicy | None = None,
    registry: VersionRegistry | None = None,
) -> dict[str, Any]:
    """Synchronous adapter for CLI and LangChain's sync Runnable API."""

    return asyncio.run(
        run_decision_async(
            request,
            model_output=model_output,
            evidence_results=evidence_results,
            approval=approval,
            feedback=feedback,
            evaluation_report=evaluation_report,
            policy=policy,
            registry=registry,
        )
    )


async def run_decision_async(
    request: DecisionRequest,
    *,
    model_output: dict[str, Any],
    evidence_results: list[ToolResult] | None = None,
    approval: str | None = None,
    feedback: FeedbackRecord | None = None,
    evaluation_report: EvaluationReport | dict[str, Any] | None = None,
    policy: DecisionPolicy | None = None,
    registry: VersionRegistry | None = None,
) -> dict[str, Any]:
    policy = policy or DecisionPolicy()
    registry = registry or VersionRegistry(initial_parameters=policy.initial_parameters)
    graph = build_graph(policy=policy, registry=registry)
    initial: DecisionState = {
        "request": request.model_dump(mode="json"),
        "model_output": model_output,
        "evidence_results": [
            item.model_dump(mode="json") for item in (evidence_results or [])
        ],
        "execution": None,
        "candidate": None,
        "active_version": registry.active.model_dump(mode="json"),
        "version_history": [],
        "evolution_status": "not_started",
        "approval": approval,
        "feedback": feedback.model_dump(mode="json") if feedback else None,
        "evaluation_report": (
            evaluation_report.model_dump(mode="json")
            if isinstance(evaluation_report, EvaluationReport)
            else evaluation_report
        ),
        "trace": [],
    }
    return await graph.ainvoke(initial)
