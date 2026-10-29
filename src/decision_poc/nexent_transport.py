"""HTTP transport for the Nexent northbound chat API.

`NexentAdapter` deliberately avoids network I/O; this module is the single
place that talks to a live Nexent deployment. It speaks only the public
`/nb/v1/chat/run` SSE contract, keeps credentials in the environment, and
refuses to report success without a `final_answer` event.
"""

import json
import os
import re
import time
import uuid
from collections.abc import Callable
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from .contracts import DecisionOutput

CHAT_RUN_PATH = "/nb/v1/chat/run"

DECISION_SCHEMA_HINT = (
    '{"request_id": "<string>", "summary": "<string>", "options": [{"option_id": "<string>", '
    '"description": "<string>", "expected_impact": "<string>"}], "selected_option_id": "<string>", '
    '"confidence": <0-1 的数字>, "risk_level": "low|medium|high", "risks": ["<string>"], '
    '"evidence": [{"source": "<string>", "content": "<string>"}]}'
)


class NexentError(Exception):
    """Base class for Nexent transport failures."""


class NexentConfigError(NexentError):
    """Missing or invalid transport configuration."""


class NexentAuthError(NexentError):
    """The API key was rejected; retrying cannot help."""


class NexentConflictError(NexentError):
    """The conversation already has a running stream."""


class NexentTransportError(NexentError):
    """The request failed for a reason retrying cannot fix."""


class NexentStreamError(NexentError):
    """The stream broke, ended early, or reported an error event."""


class _Retryable(Exception):
    """Internal marker for failures worth another attempt."""

    def __init__(self, message: str, *, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


@dataclass(frozen=True)
class NexentSettings:
    base_url: str
    api_key: str
    agent_name: str

    @classmethod
    def from_env(cls) -> "NexentSettings":
        base_url = os.getenv("NEXENT_BASE_URL", "http://localhost:5013").strip()
        api_key = os.getenv("NEXENT_API_KEY", "").strip()
        agent_name = os.getenv("NEXENT_AGENT_NAME", "").strip()
        if not api_key:
            raise NexentConfigError("NEXENT_API_KEY is required for live Nexent calls")
        if not agent_name:
            raise NexentConfigError("NEXENT_AGENT_NAME is required for live Nexent calls")
        return cls(
            base_url=(base_url or "http://localhost:5013").rstrip("/"),
            api_key=api_key,
            agent_name=agent_name,
        )


def build_decision_query(payload: Mapping[str, Any]) -> str:
    """Render a decision request as a single deterministic chat query."""
    goal = str(payload.get("goal", "")).strip()
    request_id = str(payload.get("request_id", "")).strip()
    context = payload.get("context") or {}
    constraints = [str(item).strip() for item in payload.get("constraints") or []]
    context_text = json.dumps(context, ensure_ascii=False, sort_keys=True)
    lines = [
        f"决策目标：{goal}",
        f"上下文：{context_text}",
        "约束：",
    ]
    lines.extend(f"- {item}" for item in constraints if item)
    if not constraints:
        lines.append("- 无")
    lines.extend(
        [
            "请基于本 Agent 的领域知识给出可执行决策，并严格按下面的 JSON 结构输出：",
            DECISION_SCHEMA_HINT,
            f"其中 request_id 固定填 \"{request_id}\"。",
            "只输出这段 JSON 本身，不要 Markdown 代码块、不要解释、不要输出任何工具调用标记。",
        ]
    )
    return "\n".join(lines)


def _strip_code_fence(text: str) -> str:
    body = text.strip()
    if not body.startswith("```"):
        return body
    lines = body.splitlines()
    if len(lines) >= 2 and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines).strip()


def _match_brace(text: str, start: int) -> str | None:
    """Return the balanced `{...}` starting at `start`, ignoring braces in strings."""
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None


def _iter_json_blocks(text: str):
    """Yield every balanced top-level `{...}` block in order.

    Live agents sometimes wrap the answer in prose or leak tool-call markers
    such as `<|tool_call_start|>` into the final answer, so a strict
    `json.loads` on the whole payload is not enough. A tool-call payload can
    also look like valid JSON, so candidates are tried in order until one
    validates as a decision.
    """
    index = 0
    while True:
        start = text.find("{", index)
        if start == -1:
            return
        block = _match_brace(text, start)
        if block is None:
            index = start + 1
            continue
        yield block
        index = start + len(block)


def _strip_reasoning_tags(text: str) -> str:
    """Drop leaked reasoning wrappers observed in live answers (`</think>`)."""
    return re.sub(r"</?think[^>]*>", "", text, flags=re.I).strip()


def parse_decision_output(text: str) -> DecisionOutput:
    """Parse a strict-JSON agent answer into a DecisionOutput."""
    body = _strip_code_fence(_strip_reasoning_tags(text))
    last_error: Exception | None = None
    for candidate in [body, *_iter_json_blocks(body)]:
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError as exc:
            last_error = exc
            continue
        try:
            return DecisionOutput.model_validate(payload)
        except ValueError as exc:
            last_error = exc
            continue
    if last_error is None:
        raise ValueError("agent answer contains no JSON object")
    raise last_error


def _text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return json.dumps(content, ensure_ascii=False)


def _int_or_none(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _retry_after(response: httpx.Response) -> float | None:
    raw = response.headers.get("Retry-After")
    if not raw:
        return None
    try:
        return max(0.0, float(raw.strip()))
    except ValueError:
        return None


class NexentHttpTransport:
    """Callable matching `NexentAdapter(invoke=...)` against a live Nexent."""

    def __init__(
        self,
        *,
        settings: NexentSettings,
        connect_timeout: float = 5.0,
        idle_timeout: float = 120.0,
        max_attempts: int = 2,
        backoff_seconds: float = 0.5,
        client: httpx.Client | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be >= 1")
        self._settings = settings
        self._timeout = httpx.Timeout(
            connect=connect_timeout, read=idle_timeout, write=10.0, pool=connect_timeout
        )
        self._max_attempts = max_attempts
        self._backoff = backoff_seconds
        self._sleep = sleep
        self._client = client or httpx.Client(
            timeout=self._timeout, follow_redirects=False
        )

    @property
    def endpoint(self) -> str:
        return f"{self._settings.base_url}{CHAT_RUN_PATH}"

    def close(self) -> None:
        self._client.close()

    def __call__(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        request_id = str(payload.get("request_id") or uuid.uuid4())
        headers = {
            "Authorization": f"Bearer {self._settings.api_key}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Idempotency-Key": request_id,
            "X-Request-Id": request_id,
        }
        body = self._build_body(payload)
        last_error: Exception | None = None
        for attempt in range(1, self._max_attempts + 1):
            try:
                return self._attempt(body, headers, request_id)
            except _Retryable as exc:
                last_error = exc
                if attempt >= self._max_attempts:
                    break
                delay = self._backoff * attempt
                if exc.retry_after is not None:
                    delay = max(delay, exc.retry_after)
                self._sleep(delay)
        raise NexentTransportError(
            f"Nexent request failed after {self._max_attempts} attempt(s): {last_error}"
        ) from last_error

    def _build_body(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        # `metadata` is gated per agent by `allow_chat_metadata`; when that flag is
        # false the platform answers 400. `meta_data` is audit-only and always
        # accepted, so the decision id travels there instead of the agent context.
        body: dict[str, Any] = {
            "agent_name": self._settings.agent_name,
            "query": build_decision_query(payload),
            "meta_data": {
                "decision_request_id": str(payload.get("request_id") or ""),
            },
        }
        conversation_id = payload.get("conversation_id")
        if conversation_id is not None:
            body["conversation_id"] = conversation_id
        return body

    def _attempt(
        self,
        body: dict[str, Any],
        headers: dict[str, str],
        request_id: str,
    ) -> dict[str, Any]:
        state = {"started": False}
        try:
            with self._client.stream(
                "POST",
                self.endpoint,
                json=body,
                headers=headers,
                timeout=self._timeout,
            ) as response:
                self._raise_for_status(response)
                return self._consume(response, request_id, state)
        except httpx.TimeoutException as exc:
            if state["started"]:
                raise NexentStreamError(
                    "Nexent stream stalled after it started"
                ) from exc
            raise _Retryable("Nexent request timed out before any event") from exc
        except httpx.TransportError as exc:
            if state["started"]:
                raise NexentStreamError(
                    f"Nexent stream broke after it started: {exc}"
                ) from exc
            raise _Retryable(f"Nexent connection failed: {exc}") from exc

    def _raise_for_status(self, response: httpx.Response) -> None:
        status = response.status_code
        if status == httpx.codes.OK:
            stream_status = response.headers.get("X-Stream-Status", "").lower()
            if stream_status == "conflict":
                raise NexentConflictError(
                    "Nexent reports a running stream for this conversation; "
                    "keep the draft and retry later instead of auto-retrying"
                )
            return
        if status == httpx.codes.UNAUTHORIZED:
            raise NexentAuthError("Nexent rejected the API key (HTTP 401)")
        if status == httpx.codes.TOO_MANY_REQUESTS:
            raise _Retryable(
                "Nexent rate limit or duplicate running request (HTTP 429)",
                retry_after=self._retry_after_safe(response),
            )
        if status in {502, 503, 504}:
            raise _Retryable(
                f"Nexent upstream unavailable (HTTP {status})",
                retry_after=self._retry_after_safe(response),
            )
        raise NexentTransportError(
            f"Nexent returned HTTP {status}: {_safe_body(response)}"
        )

    def _retry_after_safe(self, response: httpx.Response) -> float | None:
        try:
            return _retry_after(response)
        except Exception:
            return None

    def _consume(
        self,
        response: httpx.Response,
        request_id: str,
        state: dict[str, bool],
    ) -> dict[str, Any]:
        conversation_id = _int_or_none(response.headers.get("conversation_id"))
        final_answer: str | None = None
        events: dict[str, int] = {}
        for line in response.iter_lines():
            if not line.startswith("data:"):
                continue
            raw = line[len("data:") :].strip()
            if not raw or raw == "[DONE]":
                continue
            try:
                event = json.loads(raw)
            except json.JSONDecodeError as exc:
                raise NexentStreamError("malformed SSE event payload") from exc
            if not isinstance(event, dict):
                raise NexentStreamError("SSE event must be a JSON object")
            state["started"] = True
            event_type = str(event.get("type") or "")
            events[event_type] = events.get(event_type, 0) + 1
            content = event.get("content")
            if event_type == "conversation_created" and isinstance(content, dict):
                conversation_id = _int_or_none(content.get("conversation_id"))
            elif event_type == "final_answer":
                final_answer = _text(content)
                break
            elif event_type == "error":
                raise NexentStreamError(
                    f"Nexent reported an error event: {_text(content)[:200]}"
                )
        if final_answer is None:
            raise NexentStreamError(
                "Nexent stream ended without a final_answer event; refusing to "
                "report success"
            )
        decision: dict[str, Any] | None = None
        decision_error: str | None = None
        try:
            decision = parse_decision_output(final_answer).model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001 - surfaced as structured failure
            decision_error = f"{type(exc).__name__}: {exc}"
        result: dict[str, Any] = {
            "status": "accepted",
            "request_id": response.headers.get("X-Request-Id") or request_id,
            "conversation_id": conversation_id,
            "answer": final_answer,
            "decision": decision,
            "events": events,
        }
        if decision_error is not None:
            result["decision_error"] = decision_error
        return result


def _safe_body(response: httpx.Response) -> str:
    try:
        return response.read().decode("utf-8", errors="replace")[:200]
    except Exception:
        return "<unreadable body>"
