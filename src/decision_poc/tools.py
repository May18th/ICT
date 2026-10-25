from time import perf_counter
from typing import Sequence

from .contracts import ToolContext, ToolError, ToolResult, ToolStats


class SimulatedEvidenceTool:
    """Deterministic evidence tool that consumes scripted envelopes only."""

    def __init__(self, *, max_attempts: int = 2) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        self.max_attempts = max_attempts

    def run(
        self,
        *,
        request_id: str,
        scripted_results: Sequence[ToolResult],
    ) -> ToolResult:
        started = perf_counter()
        last_result: ToolResult | None = None
        attempts = 0

        for result in scripted_results[: self.max_attempts]:
            attempts += 1
            last_result = result
            if result.status != "error":
                break
            if result.error is None or not result.error.retryable:
                break

        if last_result is None:
            last_result = ToolResult(
                status="error",
                data={"request_id": request_id},
                error=ToolError(
                    code="NO_SCRIPTED_RESULT",
                    message="no simulated result was provided",
                    retryable=False,
                ),
                stats=ToolStats(attempt=1, latency_ms=0),
                context=ToolContext(source="mock://evidence", version=None),
            )
            attempts = 1

        data = {**last_result.data, "request_id": request_id}
        latency_ms = round((perf_counter() - started) * 1000)
        return last_result.model_copy(
            update={
                "data": data,
                "stats": ToolStats(attempt=attempts, latency_ms=latency_ms),
            }
        )


class SimulatedActionTool:
    """Return an execution envelope without performing a real side effect."""

    def run(self, *, request_id: str, option_id: str) -> ToolResult:
        return ToolResult(
            status="ok",
            data={
                "request_id": request_id,
                "option_id": option_id,
                "simulated": True,
            },
            error=None,
            stats=ToolStats(attempt=1, latency_ms=0),
            context=ToolContext(source="mock://action", version="v1"),
        )
