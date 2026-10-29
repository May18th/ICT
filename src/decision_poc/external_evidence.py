"""Normalize externally sourced citations into the existing evidence envelope."""

from collections.abc import Iterable, Mapping
from typing import Any

from .contracts import ToolContext, ToolResult, ToolStats


def external_items_to_tool_result(
    *, request_id: str, items: Iterable[Mapping[str, Any]]
) -> ToolResult:
    normalized: list[dict[str, Any]] = []
    for item in items:
        source = str(item.get("source", "")).strip()
        content = str(item.get("content", "")).strip()
        if not source or not content:
            raise ValueError("external evidence requires source and content")
        entry = {"source": source, "content": content}
        if item.get("version") is not None:
            entry["version"] = str(item["version"])
        if item.get("locator") is not None:
            entry["locator"] = str(item["locator"])
        normalized.append(entry)
    if not normalized:
        raise ValueError("external evidence requires at least one item")
    return ToolResult(
        status="ok",
        data={"items": normalized},
        error=None,
        stats=ToolStats(attempt=1, latency_ms=0),
        context=ToolContext(source=f"external://evidence/{request_id}", version="v1"),
    )

