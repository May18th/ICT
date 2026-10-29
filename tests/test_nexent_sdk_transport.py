import json
import sys
import types
from typing import Any

import pytest

from decision_poc.contracts import DecisionRequest
from decision_poc.nexent_adapter import NexentAdapter
from decision_poc.nexent_sdk_transport import NexentSdkTransport
from decision_poc.nexent_transport import NexentConfigError, NexentStreamError

MODEL_CONFIG = {
    "model_id": "glm-5.2",
    "api_key": "sk-test",
    "api_base": "https://api.example.com/v1",
}


def _decision() -> dict[str, Any]:
    return {
        "request_id": "req-1",
        "summary": "choose",
        "options": [{"option_id": "a", "description": "d", "expected_impact": "i"}],
        "selected_option_id": "a",
        "confidence": 0.7,
        "risk_level": "low",
        "risks": ["r"],
        "evidence": [{"source": "s", "content": "c"}],
    }


def _install_fake_sdk(monkeypatch, messages: list[Any]) -> dict[str, Any]:
    captured: dict[str, Any] = {}

    class _Holder:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    class _Observer:
        def __init__(self, **kwargs: Any) -> None:
            self.__dict__.update(kwargs)

    async def agent_run(run_info: Any):
        captured["query"] = run_info.query
        captured["agent_config"] = run_info.agent_config
        for message in messages:
            yield message

    modules = {
        "nexent": types.ModuleType("nexent"),
        "nexent.core": types.ModuleType("nexent.core"),
        "nexent.core.agents": types.ModuleType("nexent.core.agents"),
        "nexent.core.utils": types.ModuleType("nexent.core.utils"),
        "nexent.core.agents.run_agent": types.ModuleType(
            "nexent.core.agents.run_agent"
        ),
        "nexent.core.agents.agent_model": types.ModuleType(
            "nexent.core.agents.agent_model"
        ),
        "nexent.core.utils.observer": types.ModuleType("nexent.core.utils.observer"),
    }
    modules["nexent.core.agents.run_agent"].agent_run = agent_run
    for name in ("AgentConfig", "AgentRunInfo", "ModelConfig"):
        setattr(modules["nexent.core.agents.agent_model"], name, _Holder)
    modules["nexent.core.utils.observer"].MessageObserver = _Observer
    modules["nexent"].core = modules["nexent.core"]
    modules["nexent.core"].agents = modules["nexent.core.agents"]
    modules["nexent.core"].utils = modules["nexent.core.utils"]
    modules["nexent.core.agents"].run_agent = modules["nexent.core.agents.run_agent"]
    modules["nexent.core.agents"].agent_model = modules[
        "nexent.core.agents.agent_model"
    ]
    modules["nexent.core.utils"].observer = modules["nexent.core.utils.observer"]

    for name, module in modules.items():
        monkeypatch.setitem(sys.modules, name, module)
    return captured


def _payload() -> dict[str, Any]:
    return DecisionRequest(
        request_id="req-1", goal="choose", context={}, constraints=["safe"]
    ).model_dump(mode="json")


def test_sdk_transport_returns_decision(monkeypatch):
    captured = _install_fake_sdk(
        monkeypatch,
        [
            {"type": "model_output_thinking", "content": "思考"},
            {
                "type": "final_answer",
                "content": json.dumps(_decision(), ensure_ascii=False),
            },
        ],
    )

    transport = NexentSdkTransport(**MODEL_CONFIG)
    result = NexentAdapter(invoke=transport).run(
        DecisionRequest(
            request_id="req-1", goal="choose", context={}, constraints=["safe"]
        )
    )

    assert result["status"] == "accepted"
    assert result["conversation_id"] is None
    assert result["decision"]["selected_option_id"] == "a"
    assert "decision_error" not in result
    assert "决策目标：choose" in captured["query"]
    assert captured["agent_config"].max_steps == 5


def test_sdk_transport_rejects_stream_without_final_answer(monkeypatch):
    _install_fake_sdk(
        monkeypatch, [{"type": "model_output_thinking", "content": "thinking"}]
    )

    with pytest.raises(NexentStreamError):
        NexentSdkTransport(**MODEL_CONFIG)(_payload())


def test_sdk_transport_maps_error_event(monkeypatch):
    _install_fake_sdk(
        monkeypatch,
        [
            {"type": "error", "content": "model unavailable"},
            {"type": "final_answer", "content": "{}"},
        ],
    )

    with pytest.raises(NexentStreamError):
        NexentSdkTransport(**MODEL_CONFIG)(_payload())


def test_sdk_transport_reports_missing_sdk(monkeypatch):
    for name in list(sys.modules):
        if name == "nexent" or name.startswith("nexent."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    with pytest.raises(NexentConfigError) as excinfo:
        NexentSdkTransport(**MODEL_CONFIG)

    assert "nexent" in str(excinfo.value)


def test_sdk_transport_requires_model_credentials():
    with pytest.raises(NexentConfigError) as excinfo:
        NexentSdkTransport(model_id="", api_key="k", api_base="https://x/v1")

    assert "model_id" in str(excinfo.value)
