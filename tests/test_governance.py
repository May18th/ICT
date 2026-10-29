"""P0 freeze tests: ids, error vocabulary, governance metadata, health check."""

from datetime import UTC, datetime, timedelta

import pytest

from decision_poc.governance import (
    AssetGovernance,
    AssetStatus,
    ErrorCode,
    KnowledgeVersion,
    is_retryable,
    make_tool_error,
    stable_id,
    validate_id,
)
from decision_poc.health import SCHEMA_VERSION, health_check

NOW = datetime(2026, 10, 29, 9, 0, tzinfo=UTC)


def _governance(**overrides) -> AssetGovernance:
    payload = {
        "asset_type": "emergency_plan",
        "title": "防汛应急预案",
        "source_uri": "fixture://plan/v2",
        "source_org": "ORG-EMERG",
        "published_at": NOW,
        "valid_from": NOW,
        "content_hash": "a" * 64,
    }
    payload.update(overrides)
    return AssetGovernance(**payload)


def test_stable_id_is_deterministic_and_kind_scoped() -> None:
    first = stable_id("request", "暴雨内涝", "R-101")
    second = stable_id("request", "暴雨内涝", "R-101")
    assert first == second
    assert first.startswith("req-")
    assert stable_id("trace", "暴雨内涝", "R-101") != first


def test_stable_id_rejects_empty_parts() -> None:
    with pytest.raises(ValueError):
        stable_id("request", "", "  ")


def test_validate_id_enforces_prefix() -> None:
    good = stable_id("claim", "claim", "1")
    assert validate_id(good, kind="claim") == good
    with pytest.raises(ValueError):
        validate_id(good, kind="decision")
    with pytest.raises(ValueError):
        validate_id("bad id", kind="request")


def test_knowledge_version_orders_numerically() -> None:
    assert KnowledgeVersion(value="kb-v1.10.0").newer_than(KnowledgeVersion(value="kb-v1.9.0"))
    assert KnowledgeVersion(value="kb-v1.9.0").parts == (1, 9, 0)
    with pytest.raises(ValueError):
        KnowledgeVersion(value="1.0.0")


def test_error_code_retryability_is_closed() -> None:
    assert is_retryable(ErrorCode.UPSTREAM_UNAVAILABLE)
    assert not is_retryable(ErrorCode.PERMISSION_DENIED)
    with pytest.raises(ValueError):
        is_retryable("SOMETHING_NEW")


def test_make_tool_error_derives_retryable() -> None:
    error = make_tool_error(ErrorCode.EVIDENCE_INSUFFICIENT, "缺少管线埋深证据")
    assert error.retryable is False
    assert make_tool_error(ErrorCode.UPSTREAM_UNAVAILABLE, "tool down").retryable is True
    with pytest.raises(ValueError):
        make_tool_error(ErrorCode.NOT_FOUND, "   ")


def test_governance_validity_and_visibility() -> None:
    item = _governance(visibility=["ORG-DRAIN"], valid_to=NOW + timedelta(hours=24))
    assert item.is_valid_at(NOW + timedelta(hours=1))
    assert not item.is_valid_at(NOW + timedelta(hours=24))
    assert item.is_visible_to(["ORG-DRAIN"])
    assert not item.is_visible_to(["ORG-GAS"])


def test_governance_rejects_inverted_interval_and_naive_time() -> None:
    with pytest.raises(ValueError):
        _governance(valid_to=NOW - timedelta(seconds=1))
    with pytest.raises(ValueError):
        _governance(published_at=datetime(2026, 10, 29, 9, 0))


def test_governance_requires_published_for_citation() -> None:
    assert _governance().is_published()
    assert not _governance(status=AssetStatus.DRAFT).is_published()
    assert not _governance(status=AssetStatus.DEPRECATED).is_published()


def test_governance_rejects_bad_hash() -> None:
    with pytest.raises(ValueError):
        _governance(content_hash="not-a-hash")


def test_health_check_passes_without_dataset() -> None:
    report = health_check()
    assert report["schema_version"] == SCHEMA_VERSION
    assert report["status"] in {"ok", "degraded"}
    assert all(check["status"] != "fail" for check in report["checks"])


def test_health_check_reports_missing_dataset_as_warning(tmp_path) -> None:
    report = health_check(tmp_path / "absent")
    dataset = next(c for c in report["checks"] if c["name"] == "dataset")
    assert dataset["status"] == "warn"
    assert report["status"] == "degraded"
