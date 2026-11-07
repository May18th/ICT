import json
from typing import Any

from pydantic_ai import Agent
from pydantic_ai.models.test import TestModel

from .contracts import DecisionOutput, DecisionRequest
from .model_provider import (
    PROVIDER_ENV,
    ModelProviderError,
    ModelSettings,
    build_model,
    settings_from_env,
)


def _build_agent() -> Agent:
    return Agent(
        output_type=DecisionOutput,
        instructions=(
            "Generate a domain-neutral decision using only the supplied request "
            "and evidence. Return a selected option, explicit risks, and evidence. "
            "Copy request_id exactly from the request. "
            "If the evidence cannot support any action, set refused=true and give a "
            "refusal_reason; otherwise leave refused=false and refusal_reason null."
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


async def generate_decision_with_model(
    request: DecisionRequest,
    evidence: list[dict[str, Any]],
    model: Any,
) -> DecisionOutput:
    """Run the typed agent with an explicitly supplied pydantic-ai model."""

    result = await _build_agent().run(_prompt(request, evidence), model=model)
    return result.output


async def generate_decision_live(
    request: DecisionRequest,
    evidence: list[dict[str, Any]],
    settings: ModelSettings | None = None,
) -> DecisionOutput:
    """Run the typed agent with the configured real provider (offline by default)."""

    resolved = settings if settings is not None else settings_from_env()
    model = build_model(resolved)
    if model is None:
        raise ModelProviderError(
            f"{PROVIDER_ENV} is not set to a real provider; use "
            "generate_decision_async for the offline TestModel path"
        )
    return await generate_decision_with_model(request, evidence, model)
