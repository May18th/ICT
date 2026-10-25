from typing import Any

from langchain_core.runnables import RunnableLambda

from .contracts import DecisionInput
from .graph import run_decision


def build_demo_runnable(*, model_output: dict[str, Any]) -> RunnableLambda:
    """Expose the decision graph as a LangChain demo/evaluation runnable."""

    def invoke(payload: dict[str, Any]) -> dict[str, Any]:
        request = DecisionInput.model_validate(payload)
        result = run_decision(request, model_output=model_output)
        return {
            "decision": result["decision"],
            "trace": result["trace"],
            "termination_reason": result["termination_reason"],
        }

    return RunnableLambda(invoke)
