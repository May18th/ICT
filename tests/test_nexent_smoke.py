import io
import json

import httpx

from decision_poc.nexent_smoke import missing_env, run
from decision_poc.nexent_transport import (
    NexentHttpTransport,
    NexentSettings,
    NexentStreamError,
)

SETTINGS = NexentSettings(
    base_url="https://nexent.example.com",
    api_key="key-1",
    agent_name="decision-agent",
)


def test_missing_env_lists_both_credentials():
    assert missing_env({}) == ["NEXENT_API_KEY", "NEXENT_AGENT_NAME"]
    assert missing_env({"NEXENT_API_KEY": "k", "NEXENT_AGENT_NAME": " "}) == [
        "NEXENT_AGENT_NAME"
    ]


def test_smoke_exits_2_without_credentials(monkeypatch):
    monkeypatch.delenv("NEXENT_API_KEY", raising=False)
    monkeypatch.delenv("NEXENT_AGENT_NAME", raising=False)
    out = io.StringIO()

    assert run(out=out) == 2
    assert "NEXENT_API_KEY" in out.getvalue()


def test_smoke_succeeds_with_injected_transport():
    decision = {
        "request_id": "req-smoke",
        "summary": "ok",
        "options": [{"option_id": "a", "description": "d", "expected_impact": "i"}],
        "selected_option_id": "a",
        "confidence": 0.5,
        "risk_level": "low",
        "risks": ["r"],
        "evidence": [{"source": "s", "content": "c"}],
    }
    event = {"type": "final_answer", "content": json.dumps(decision, ensure_ascii=False)}

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            headers={"content-type": "text/event-stream"},
            content=f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8"),
        )

    transport = NexentHttpTransport(
        settings=SETTINGS,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
        sleep=lambda _seconds: None,
    )
    out = io.StringIO()

    assert run(settings=SETTINGS, transport=transport, out=out) == 0
    payload = json.loads(out.getvalue().split("\n提示：")[0])
    assert payload["status"] == "accepted"
    assert payload["decision"]["selected_option_id"] == "a"


def test_smoke_reports_transport_failure():
    class Broken:
        def __call__(self, _payload):
            raise NexentStreamError("stream ended without a final_answer event")

    out = io.StringIO()

    assert run(settings=SETTINGS, transport=Broken(), out=out) == 1
    assert "NexentStreamError" in out.getvalue()
