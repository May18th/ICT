"""Minimal health check: proves the skeleton imports and schemas validate.

P0 acceptance says "empty dataset can start". So this module never requires data
files to exist; a missing dataset is reported as `warn`, not `fail`. It performs
no network I/O and no writes.

    python -m decision_poc.health
    python -m decision_poc.health --dataset samples/assets/urban
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import (
    DecisionOption,
    DecisionOutput,
    DecisionRequest,
    EvidenceItem,
    RiskLevel,
    ToolError,
    ToolResult,
    ToolContext,
    ToolStats,
)
from .governance import (
    AssetGovernance,
    ErrorCode,
    KnowledgeVersion,
    is_retryable,
    stable_id,
)

SCHEMA_VERSION = "p0-1"
PYTHON_MIN = (3, 12)
DATA_SUFFIXES = {".csv", ".md", ".png", ".json", ".txt"}


def _check(name: str, fn) -> dict[str, Any]:
    try:
        detail = fn()
        return {"name": name, "status": "ok", "detail": detail}
    except Exception as exc:  # noqa: BLE001 - health check must report, not raise
        return {"name": name, "status": "fail", "detail": f"{type(exc).__name__}: {exc}"}


def _python_version() -> str:
    if sys.version_info[:2] < PYTHON_MIN:
        raise RuntimeError(f"requires python >= {PYTHON_MIN}, got {sys.version.split()[0]}")
    return sys.version.split()[0]


def _decision_contracts() -> str:
    request = DecisionRequest(
        request_id=stable_id("request", "health", "req"),
        goal="health check",
        context={"probe": True},
        constraints=["offline"],
    )
    output = DecisionOutput(
        request_id=request.request_id,
        summary="ok",
        options=[DecisionOption(option_id="o1", description="d", expected_impact="none")],
        selected_option_id="o1",
        confidence=0.5,
        risk_level=RiskLevel.LOW,
        risks=["none"],
        evidence=[EvidenceItem(source="health", content="probe")],
    )
    return f"round-trip ok ({output.request_id})"


def _tool_envelope() -> str:
    result = ToolResult(
        status="error",
        data={},
        error=ToolError(code=str(ErrorCode.NOT_FOUND), message="probe", retryable=False),
        stats=ToolStats(attempt=1, latency_ms=0),
        context=ToolContext(source="health"),
    )
    return f"envelope ok ({result.error.code})"  # type: ignore[union-attr]


def _governance_schema() -> str:
    governance = AssetGovernance(
        asset_type="emergency_plan",
        title="health probe",
        source_uri="memory://health",
        source_org="ORG-TEST",
        published_at=datetime(2026, 1, 1, tzinfo=UTC),
        valid_from=datetime(2026, 1, 1, tzinfo=UTC),
        content_hash="0" * 64,
        visibility=[],
    )
    assert governance.is_valid_at(datetime(2026, 6, 1, tzinfo=UTC))
    assert governance.is_visible_to([])
    return f"governance ok ({KnowledgeVersion(value='kb-v1.0.0').parts})"


def _error_vocabulary() -> str:
    retryable = [code.value for code in ErrorCode if is_retryable(code)]
    return f"{len(ErrorCode)} codes, retryable={sorted(retryable)}"


def _dataset(dataset_dir: Path | None) -> dict[str, Any]:
    if dataset_dir is None:
        return {"status": "skipped", "detail": "no --dataset given"}
    if not dataset_dir.is_dir():
        return {"status": "warn", "detail": f"dataset dir missing: {dataset_dir}"}
    files = [
        path
        for path in sorted(dataset_dir.rglob("*"))
        if path.is_file() and path.suffix.lower() in DATA_SUFFIXES
    ]
    manifest = dataset_dir / "manifest.json"
    detail: dict[str, Any] = {
        "files": len([p for p in files if p.name != "manifest.json"]),
        "manifest": manifest.exists(),
    }
    if manifest.exists():
        payload = json.loads(manifest.read_text(encoding="utf-8"))
        detail["assets"] = payload.get("asset_count", 0)
    status = "ok" if detail["files"] else "warn"
    return {"status": status, "detail": detail}


def health_check(dataset_dir: Path | str | None = None) -> dict[str, Any]:
    target = Path(dataset_dir) if dataset_dir is not None else None
    checks = [
        _check("python_version", _python_version),
        _check("decision_contracts", _decision_contracts),
        _check("tool_envelope", _tool_envelope),
        _check("governance_schema", _governance_schema),
        _check("error_vocabulary", _error_vocabulary),
        {"name": "dataset", **_dataset(target)},
        {
            "name": "network",
            "status": "ok",
            "detail": "offline by design; no network calls in P0-P4",
        },
    ]
    statuses = {check["status"] for check in checks}
    overall = "fail" if "fail" in statuses else "degraded" if "warn" in statuses else "ok"
    return {"schema_version": SCHEMA_VERSION, "status": overall, "checks": checks}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="decision_poc health check")
    parser.add_argument("--dataset", default=None, help="optional dataset directory")
    args = parser.parse_args(argv)
    report = health_check(args.dataset)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 1 if report["status"] == "fail" else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
