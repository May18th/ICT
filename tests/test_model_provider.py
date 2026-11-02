import asyncio

import pytest
from pydantic_ai.models.test import TestModel

from decision_poc.agent import (
    generate_decision_async,
    generate_decision_live,
    generate_decision_with_model,
)
from decision_poc.contracts import DecisionRequest
from decision_poc.model_provider import (
    API_KEY_ENV,
    DEFAULT_DEEPSEEK_MODEL,
    MODEL_ENV,
    PROVIDER_ENV,
    ModelProviderError,
    ModelSettings,
    build_model,
    settings_from_env,
)

REQUEST = DecisionRequest(
    request_id="req-provider-001",
    goal="Choose a safe next action",
    context={"signal": "anomaly"},
    constraints=["preserve evidence"],
)

EVIDENCE = [
    {"source": "mock://evidence", "content": "Observed anomaly", "version": "v1"}
]

MODEL_OUTPUT = {
    "request_id": "req-provider-001",
    "summary": "A safe generic decision",
    "options": [
        {
            "option_id": "opt-1",
            "description": "Inspect evidence",
            "expected_impact": "Improves confidence",
        }
    ],
    "selected_option_id": "opt-1",
    "confidence": 0.82,
    "risk_level": "low",
    "risks": ["The signal may be incomplete"],
    "evidence": EVIDENCE,
}


def test_default_settings_stay_offline() -> None:
    settings = settings_from_env({})

    assert settings.provider == "test"
    assert settings.model_name == DEFAULT_DEEPSEEK_MODEL
    assert settings.is_real is False
    assert build_model(settings, {}) is None


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ModelProviderError, match=PROVIDER_ENV):
        settings_from_env({PROVIDER_ENV: "openai"})


def test_unknown_model_is_rejected() -> None:
    with pytest.raises(ModelProviderError, match=MODEL_ENV):
        settings_from_env({PROVIDER_ENV: "deepseek", MODEL_ENV: "gpt-4o"})


def test_deepseek_requires_api_key_without_leaking_it() -> None:
    settings = settings_from_env({PROVIDER_ENV: "deepseek"})

    with pytest.raises(ModelProviderError, match=API_KEY_ENV) as excinfo:
        build_model(settings, {PROVIDER_ENV: "deepseek"})

    assert "sk-" not in str(excinfo.value)
    assert API_KEY_ENV in str(excinfo.value)


def test_live_call_refuses_offline_settings() -> None:
    with pytest.raises(ModelProviderError, match="not set to a real provider"):
        asyncio.run(generate_decision_live(REQUEST, EVIDENCE, ModelSettings()))


def test_deepseek_model_can_be_built_without_network() -> None:
    pytest.importorskip("openai")
    settings = settings_from_env({PROVIDER_ENV: "deepseek"})

    model = build_model(settings, {API_KEY_ENV: "unit-test-key"})

    assert model is not None
    assert getattr(model, "model_name", None) == "deepseek-chat"


def test_explicit_test_model_keeps_deterministic_path() -> None:
    decision = asyncio.run(
        generate_decision_with_model(
            REQUEST,
            EVIDENCE,
            TestModel(custom_output_args=MODEL_OUTPUT),
        )
    )

    assert decision.request_id == REQUEST.request_id
    assert decision.selected_option_id == "opt-1"


def test_default_async_path_still_uses_test_model_output() -> None:
    decision = asyncio.run(generate_decision_async(REQUEST, EVIDENCE, MODEL_OUTPUT))

    assert decision.request_id == REQUEST.request_id
