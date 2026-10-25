from decision_poc.contracts import ToolContext, ToolError, ToolResult, ToolStats
from decision_poc.tools import SimulatedEvidenceTool


def _result(*, status="error", retryable=True, content=None):
    return ToolResult(
        status=status,
        data={"items": content or []},
        error=(
            ToolError(code="TEMPORARY", message="temporary error", retryable=retryable)
            if status == "error"
            else None
        ),
        stats=ToolStats(attempt=1, latency_ms=0),
        context=ToolContext(source="mock://evidence", version="v1"),
    )


def test_transient_tool_error_retries_until_success():
    tool = SimulatedEvidenceTool(max_attempts=3)
    success = _result(status="ok", content=[{"source": "mock", "content": "fact"}])

    result = tool.run(
        request_id="req-1",
        scripted_results=[_result(), success],
    )

    assert result.status == "ok"
    assert result.stats.attempt == 2
    assert result.data["items"] == [{"source": "mock", "content": "fact"}]


def test_tool_retries_are_bounded_and_preserve_final_error():
    tool = SimulatedEvidenceTool(max_attempts=2)

    result = tool.run(
        request_id="req-2",
        scripted_results=[_result(), _result(), _result()],
    )

    assert result.status == "error"
    assert result.error.code == "TEMPORARY"
    assert result.stats.attempt == 2


def test_non_retryable_tool_error_is_not_retried():
    tool = SimulatedEvidenceTool(max_attempts=4)

    result = tool.run(
        request_id="req-3",
        scripted_results=[_result(retryable=False), _result(status="ok")],
    )

    assert result.status == "error"
    assert result.error.retryable is False
    assert result.stats.attempt == 1
