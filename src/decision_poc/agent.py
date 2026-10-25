import json
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from .contracts import DecisionOutput, DecisionRequest


def _build_agent() -> Agent:
    return Agent(
        output_type=DecisionOutput,
        instructions=(
            "Generate a domain-neutral decision using only the supplied request "
            "and evidence. Return a selected option, explicit risks, and evidence. "
            "Copy request_id exactly from the request."
        ),
    )


def _prompt(request: DecisionRequest, evidence: list[dict[str, Any]]) -> str:
    return json.dumps(
        {"request": request.model_dump(mode="json"), "evidence": evidence},
        ensure_ascii=False,
        sort_keys=True,
    )


async def generate_decision_async(
    request: DecisionRequest,
    evidence: list[dict[str, Any]],
    model_output: dict[str, Any],
) -> DecisionOutput:
    """Run the typed agent with a deterministic, credential-free test model."""

    result = await _build_agent().run(
        _prompt(request, evidence),
        model=TestModel(custom_output_args=model_output),
    )
    return result.output
