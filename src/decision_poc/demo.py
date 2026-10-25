from typing import Any

from langchain_core.runnables import RunnableLambda

from .contracts import DecisionPolicy, DecisionRequest, EvaluationReport, FeedbackRecord, ToolResult
from .evolution import VersionRegistry
from .graph import run_decision


def build_demo_runnable(
    *,
    model_output: dict[str, Any],
    evidence_results: list[ToolResult] | None = None,
    approval: str | None = None,
    feedback: FeedbackRecord | None = None,
    evaluation_report: EvaluationReport | dict[str, Any] | None = None,
    policy: DecisionPolicy | None = None,
    registry: VersionRegistry | None = None,
) -> RunnableLambda:
    """Expose the LangGraph flow as a thin LangChain demo/evaluation adapter."""

    def invoke(payload: dict[str, Any]) -> dict[str, Any]:
        request = DecisionRequest.model_validate(payload)
        result = run_decision(
            request,
            model_output=model_output,
            evidence_results=evidence_results,
            approval=approval,
            feedback=feedback,
            evaluation_report=evaluation_report,
            policy=policy,
            registry=registry,
        )
        # LangChain is a thin transport/evaluation adapter; graph behavior stays in LangGraph.
        return result

    return RunnableLambda(invoke)
