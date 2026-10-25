import asyncio
from typing import Any

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from .agent import generate_decision_async
from .contracts import DecisionInput, DecisionOutput


class DecisionState(TypedDict, total=False):
    request: dict[str, Any]
    model_output: dict[str, Any]
    decision: dict[str, Any]
    trace: list[str]
    termination_reason: str


async def _generate_node(state: DecisionState) -> dict[str, Any]:
    request = DecisionInput.model_validate(state["request"])
    decision = await generate_decision_async(request, state["model_output"])
    return {
        "decision": decision.model_dump(mode="json"),
        "trace": [*state.get("trace", []), "decision_generated"],
    }


def _validate_node(state: DecisionState) -> dict[str, Any]:
    request = DecisionInput.model_validate(state["request"])
    decision = DecisionOutput.model_validate(state["decision"])
    if decision.incident_id != request.incident_id:
        raise ValueError(
            "incident_id mismatch: "
            f"expected {request.incident_id}, got {decision.incident_id}"
        )
    if not decision.evidence or not decision.risks:
        raise ValueError("decision must contain evidence and risks")
    return {
        "trace": [*state.get("trace", []), "decision_validated"],
        "termination_reason": "completed",
    }


def build_graph():
    graph = StateGraph(DecisionState)
    graph.add_node("generate_decision", _generate_node)
    graph.add_node("validate_decision", _validate_node)
    graph.add_edge(START, "generate_decision")
    graph.add_edge("generate_decision", "validate_decision")
    graph.add_edge("validate_decision", END)
    return graph.compile()


def run_decision(
    request: DecisionInput,
    *,
    model_output: dict[str, Any],
) -> dict[str, Any]:
    """Synchronous adapter for CLI and LangChain's sync Runnable API."""

    return asyncio.run(run_decision_async(request, model_output=model_output))


async def run_decision_async(
    request: DecisionInput,
    *,
    model_output: dict[str, Any],
) -> dict[str, Any]:
    graph = build_graph()
    return await graph.ainvoke(
        {
            "request": request.model_dump(mode="json"),
            "model_output": model_output,
            "trace": [],
        }
    )
