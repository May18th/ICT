"""Optional real-model provider for the typed decision node.

Design constraints (see ``docs/real-model-provider-design.md``):

- offline by default: when ``DECISION_LLM_PROVIDER`` is unset the typed decision
  node keeps using Pydantic AI's ``TestModel`` — no credentials, no network, and
  the reproducible ``pytest`` / ``scripts/reproduce.py`` paths stay unchanged;
- the real provider is built only when ``DECISION_LLM_PROVIDER=deepseek`` and the
  API key is supplied through the environment;
- this module never echoes the API key in exception messages.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, Mapping

SUPPORTED_PROVIDERS: tuple[str, ...] = ("test", "deepseek")
DEFAULT_DEEPSEEK_MODEL = "deepseek-chat"
SUPPORTED_DEEPSEEK_MODELS: tuple[str, ...] = (
    "deepseek-chat",
    "deepseek-reasoner",
    "deepseek-v4-flash",
    "deepseek-v4-pro",
)

PROVIDER_ENV = "DECISION_LLM_PROVIDER"
MODEL_ENV = "DEEPSEEK_MODEL"
API_KEY_ENV = "DEEPSEEK_API_KEY"


class ModelProviderError(ValueError):
    """Raised when the optional real-model configuration is invalid."""


@dataclass(frozen=True)
class ModelSettings:
    """Resolved provider configuration (never carries the API key)."""

    provider: str = "test"
    model_name: str = DEFAULT_DEEPSEEK_MODEL

    @property
    def is_real(self) -> bool:
        return self.provider != "test"


def settings_from_env(env: Mapping[str, str] | None = None) -> ModelSettings:
    """Read provider settings from the environment without touching the network."""

    source = os.environ if env is None else env
    provider = (source.get(PROVIDER_ENV) or "test").strip().lower()
    if provider not in SUPPORTED_PROVIDERS:
        raise ModelProviderError(
            f"unsupported {PROVIDER_ENV}: {provider!r}; "
            f"expected one of {', '.join(SUPPORTED_PROVIDERS)}"
        )
    model_name = (source.get(MODEL_ENV) or DEFAULT_DEEPSEEK_MODEL).strip() or DEFAULT_DEEPSEEK_MODEL
    if provider == "deepseek" and model_name not in SUPPORTED_DEEPSEEK_MODELS:
        raise ModelProviderError(
            f"unsupported {MODEL_ENV}: {model_name!r}; "
            f"expected one of {', '.join(SUPPORTED_DEEPSEEK_MODELS)}"
        )
    return ModelSettings(provider=provider, model_name=model_name)


def build_model(settings: ModelSettings, env: Mapping[str, str] | None = None) -> Any | None:
    """Build a pydantic-ai model for real providers; ``None`` means the TestModel path."""

    if not settings.is_real:
        return None

    source = os.environ if env is None else env
    if settings.provider == "deepseek":
        api_key = (source.get(API_KEY_ENV) or "").strip()
        if not api_key:
            raise ModelProviderError(
                f"{PROVIDER_ENV}=deepseek requires {API_KEY_ENV} in the environment"
            )
        try:
            from pydantic_ai.models.openai import OpenAIChatModel
            from pydantic_ai.providers.deepseek import DeepSeekProvider
        except ImportError as exc:  # pragma: no cover - depends on the optional extra
            raise ModelProviderError(
                "real model provider requires the optional `openai` package: "
                "run `uv sync --group llm`"
            ) from exc
        return OpenAIChatModel(settings.model_name, provider=DeepSeekProvider(api_key=api_key))

    raise ModelProviderError(f"unsupported {PROVIDER_ENV}: {settings.provider!r}")
