"""Transport that runs Nexent as an in-process SDK instead of over HTTP.

Use this path when no Nexent platform deployment is reachable (for example a
container sandbox without a Docker daemon). It requires the optional `nexent`
package, which is deliberately not a project dependency:

    uv pip install "nexent==1.8.1"

Trade-off: this exercises the Nexent agent runtime, not the `/nb/v1`
northbound contract. The HTTP transport remains the only thing that validates
the platform API contract.
"""

import asyncio
import json
import uuid
from threading import Event
from typing import Any

from .nexent_transport import (
    NexentConfigError,
    NexentError,
    NexentStreamError,
    NexentTransportError,
    build_decision_query,
    parse_decision_output,
)

INSTALL_HINT = 'nexent SDK 未安装，执行：uv pip install "nexent==1.8.1"'
MAX_ANSWER_CHARS = 8000


def _load_sdk() -> dict[str, Any]:
    """Import the Nexent SDK lazily so offline paths stay dependency-free."""
    try:
        from nexent.core.agents.agent_model import (  # type: ignore[import-not-found]
            AgentConfig,
            AgentRunInfo,
            ModelConfig,
        )
        from nexent.core.agents.run_agent import agent_run  # type: ignore[import-not-found]
        from nexent.core.utils.observer import (  # type: ignore[import-not-found]
            MessageObserver,
        )
    except ImportError as exc:
        raise NexentConfigError(INSTALL_HINT) from exc
    return {
        "agent_run": agent_run,
        "AgentConfig": AgentConfig,
        "AgentRunInfo": AgentRunInfo,
        "ModelConfig": ModelConfig,
        "MessageObserver": MessageObserver,
    }


def _normalize_type(value: Any) -> str:
    return str(value or "").strip().lower()


class NexentSdkTransport:
    """Callable matching `NexentAdapter(invoke=...)` backed by the Nexent SDK."""

    def __init__(
        self,
        *,
        model_id: str,
        api_key: str,
        api_base: str,
        agent_name: str = "decision-agent",
        max_steps: int = 5,
        lang: str = "zh",
        timeout_seconds: float = 180.0,
    ) -> None:
        missing = [
            name
            for name, value in (
                ("model_id", model_id),
                ("api_key", api_key),
                ("api_base", api_base),
            )
            if not str(value or "").strip()
        ]
        if missing:
            raise NexentConfigError(
                "缺少模型配置：" + ", ".join(missing) + "（模型凭据由 SDK 直接持有，不走平台 API Key）"
            )
        if max_steps < 1:
            raise ValueError("max_steps must be >= 1")
        self._sdk = _load_sdk()
        self._model_id = model_id
        self._api_key = api_key
        self._api_base = api_base
        self._agent_name = agent_name
        self._max_steps = max_steps
        self._lang = lang
        self._timeout_seconds = timeout_seconds

    def __call__(self, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = str(payload.get("request_id") or uuid.uuid4())
        query = build_decision_query(payload)
        try:
            final_answer, events = asyncio.run(self._collect(query))
        except NexentError:
            raise
        except RuntimeError as exc:
            raise NexentConfigError(
                "NexentSdkTransport 不能在已运行的事件循环里同步调用；请改用线程或 asyncio 入口"
            ) from exc
        except Exception as exc:  # noqa: BLE001 - mapped to a domain error
            raise NexentTransportError(f"Nexent SDK 调用失败：{exc}") from exc

        decision: dict[str, Any] | None = None
        decision_error: str | None = None
        try:
            decision = parse_decision_output(final_answer).model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 - surfaced as structured failure
            decision_error = f"{type(exc).__name__}: {exc}"

        result: dict[str, Any] = {
            "status": "accepted",
            "request_id": request_id,
            "conversation_id": None,
            "answer": final_answer,
            "decision": decision,
            "events": events,
        }
        if decision_error is not None:
            result["decision_error"] = decision_error
        return result

    async def _collect(self, query: str) -> tuple[str, dict[str, int]]:
        sdk = self._sdk
        observer = sdk["MessageObserver"](lang=self._lang)
        model_config = sdk["ModelConfig"](
            cite_name="decision",
            api_key=self._api_key,
            model_name=self._model_id,
            url=self._api_base,
        )
        agent_config = sdk["AgentConfig"](
            name=self._agent_name,
            description="decision agent",
            tools=[],
            max_steps=self._max_steps,
            model_name=self._model_id,
        )
        run_info = sdk["AgentRunInfo"](
            query=query,
            model_config_list=[model_config],
            observer=observer,
            agent_config=agent_config,
            stop_event=Event(),
        )

        final_answer: str | None = None
        events: dict[str, int] = {}

        async def consume() -> None:
            nonlocal final_answer
            async for message in sdk["agent_run"](run_info):
                event = _decode(message)
                if event is None:
                    continue
                event_type = _normalize_type(event.get("type"))
                events[event_type] = events.get(event_type, 0) + 1
                if "final_answer" in event_type:
                    final_answer = str(event.get("content") or "")[:MAX_ANSWER_CHARS]
                    return
                if "error" in event_type:
                    raise NexentStreamError(
                        f"Nexent SDK 返回错误事件：{str(event.get('content'))[:200]}"
                    )

        await asyncio.wait_for(consume(), timeout=self._timeout_seconds)
        if final_answer is None:
            raise NexentStreamError("Nexent SDK 流结束但未产生 final_answer，拒绝标记成功")
        return final_answer, events


def _decode(message: Any) -> dict[str, Any] | None:
    if isinstance(message, dict):
        return message
    if not isinstance(message, (str, bytes)):
        return None
    try:
        decoded = json.loads(message)
    except json.JSONDecodeError:
        return None
    return decoded if isinstance(decoded, dict) else None
