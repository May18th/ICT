from typing import Any

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from .contracts import DecisionInput, DecisionOutput


def _build_agent() -> Agent:
    return Agent(
        output_type=DecisionOutput,
        instructions=(
            "Return a typed incident decision. Include evidence and risks. "
            "Do not invent an incident id; copy the request incident_id."
        ),
    )


def _prompt(request: DecisionInput) -> str:
    return (
        f"incident_id={request.incident_id}\n"
        f"service={request.service}\n"
        f"alert={request.alert}"
    )


async def generate_decision_async(
    request: DecisionInput, model_output: dict[str, Any]
) -> DecisionOutput:
    """Run the typed agent with a deterministic, credential-free test model."""

    result = await _build_agent().run(
        _prompt(request),
        model=TestModel(custom_output_args=model_output),
    )
    return result.output
