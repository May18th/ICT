"""Nexent transport boundary for the offline decision PoC.

The adapter only shapes a request and delegates transport to an injected
callable. It never performs network I/O itself, keeping the PoC offline by
default while making a future Nexent integration explicit and testable.
"""

from collections.abc import Callable
from collections.abc import Mapping
from typing import Any

from .contracts import DecisionRequest


class NexentAdapter:
    def __init__(self, *, invoke: Callable[[dict[str, Any]], dict[str, Any]]):
        self._invoke = invoke

    def run(self, request: DecisionRequest) -> dict[str, Any]:
        payload = request.model_dump(mode="json")
        result = self._invoke(payload)
        if not isinstance(result, Mapping):
            raise TypeError("Nexent transport response must be a mapping")
        return dict(result)

