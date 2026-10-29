"""P0 frozen primitives: identifiers, error codes, asset governance metadata.

Everything the eight layers share must be nailed down here before any layer is
built, otherwise every module invents its own `request_id` format and error
vocabulary. This module is deliberately dependency-free (stdlib + pydantic) and
holds no business logic.

Frozen rules
------------
- `request_id` / `trace_id` / `claim_id` / `decision_id` are *stable*: the same
  inputs always produce the same id. Determinism is what makes the demo re-run
  byte-identical and the evaluation reproducible.
- `knowledge_version` is `kb-v<major>.<minor>.<patch>`; any consumer that cannot
  parse it must reject the input rather than guess.
- Error codes are a closed enum. Unknown codes are a bug, not an extension point.
"""

from __future__ import annotations

import hashlib
import re
from datetime import datetime
from enum import StrEnum
from collections.abc import Mapping, Sequence
from typing import Any

from pydantic import Field, field_validator, model_validator

from .contracts import StrictModel, ToolError

ID_PREFIXES = {
    "request": "req",
    "trace": "trc",
    "claim": "clm",
    "decision": "dec",
    "event": "evt",
    "asset": "AS",
}

_SLUG_ALLOWED = "-_"
ID_PATTERN = r"^[a-z]{2,4}-[A-Za-z0-9_-]{1,80}$"
KNOWLEDGE_VERSION_PATTERN = r"^kb-v\d+\.\d+\.\d+$"
CHECKSUM_PATTERN = r"^[0-9a-f]{64}$"


def _slug(value: str, *, limit: int = 24) -> str:
    cleaned = "".join(
        ch if (ch.isalnum() and ch.isascii()) or ch in _SLUG_ALLOWED else "-"
        for ch in value.strip()
    )
    while "--" in cleaned:
        cleaned = cleaned.replace("--", "-")
    cleaned = cleaned.strip("-_")
    return (cleaned or "x")[:limit]


def stable_id(kind: str, *parts: str) -> str:
    """Deterministic, readable, collision-resistant id.

    Same `kind` + same `parts` -> same id, across processes and machines.
    """
    try:
        prefix = ID_PREFIXES[kind]
    except KeyError as exc:  # pragma: no cover - programming error path
        raise ValueError(f"unknown id kind: {kind}") from exc
    cleaned = [part.strip() for part in parts if part is not None]
    cleaned = [part for part in cleaned if part]
    if not cleaned:
        raise ValueError("stable_id requires at least one non-empty part")
    material = "|".join(cleaned)
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]
    head = next((slug for slug in (_slug(part) for part in cleaned) if slug != "x"), "x")
    return f"{prefix}-{head}-{digest}"


def validate_id(value: str, *, kind: str | None = None) -> str:
    text = value.strip()
    if not re.match(ID_PATTERN, text):
        raise ValueError(f"id must match {ID_PATTERN}: {value!r}")
    if kind is not None:
        expected = ID_PREFIXES[kind]
        if not text.startswith(f"{expected}-"):
            raise ValueError(f"{kind} id must start with {expected}-: {value!r}")
    return text


class IdKind(StrEnum):
    REQUEST = "request"
    TRACE = "trace"
    CLAIM = "claim"
    DECISION = "decision"
    EVENT = "event"


class KnowledgeVersion(StrictModel):
    """`kb-v1.2.0`; ordering is numeric per segment, never lexicographic."""

    value: str = Field(min_length=1, pattern=KNOWLEDGE_VERSION_PATTERN)

    @property
    def parts(self) -> tuple[int, int, int]:
        major, minor, patch = self.value.removeprefix("kb-v").split(".")
        return int(major), int(minor), int(patch)

    def newer_than(self, other: "KnowledgeVersion") -> bool:
        return self.parts > other.parts


class ErrorCode(StrEnum):
    """Closed error vocabulary shared by tools, skills and the demo."""

    VALIDATION_FAILED = "VALIDATION_FAILED"
    NOT_FOUND = "NOT_FOUND"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    EVIDENCE_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"
    EVIDENCE_CONFLICT = "EVIDENCE_CONFLICT"
    KNOWLEDGE_EXPIRED = "KNOWLEDGE_EXPIRED"
    UPSTREAM_UNAVAILABLE = "UPSTREAM_UNAVAILABLE"
    NOT_IMPLEMENTED = "NOT_IMPLEMENTED"
    NO_SCRIPTED_RESULT = "NO_SCRIPTED_RESULT"
    DECISION_INVALID = "DECISION_INVALID"


RETRYABLE_CODES = frozenset(
    {ErrorCode.UPSTREAM_UNAVAILABLE, ErrorCode.NOT_IMPLEMENTED}
)


def is_retryable(code: ErrorCode | str) -> bool:
    try:
        return ErrorCode(str(code)) in RETRYABLE_CODES
    except ValueError:
        raise ValueError(f"unknown error code: {code!r}") from None


def make_tool_error(code: ErrorCode, message: str) -> ToolError:
    """Single constructor so `retryable` is never hand-set inconsistently."""
    text = message.strip()
    if not text:
        raise ValueError("error message must not be blank")
    return ToolError(code=str(code), message=text, retryable=is_retryable(code))


class AssetStatus(StrEnum):
    DRAFT = "draft"
    PUBLISHED = "published"
    DEPRECATED = "deprecated"


class AssetGovernance(StrictModel):
    """Governance metadata required by the task brief but absent from `DataAsset`.

    `DataAsset` (P1) stays frozen because it is already tested and consumed; the
    brief-mandated fields live here and are attached as an optional component.
    Field correspondence is documented in `GOVERNANCE_FIELD_MAP`.
    """

    asset_type: str = Field(min_length=1)
    title: str = Field(min_length=1)
    source_uri: str = Field(min_length=1)
    source_org: str = Field(min_length=1)
    published_at: datetime
    valid_from: datetime
    valid_to: datetime | None = None
    visibility: list[str] = Field(default_factory=list)
    content_hash: str = Field(pattern=CHECKSUM_PATTERN)
    status: AssetStatus = AssetStatus.PUBLISHED

    @field_validator("asset_type", "title", "source_uri", "source_org")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        text = value.strip()
        if not text:
            raise ValueError("must not be blank")
        return text

    @field_validator("visibility")
    @classmethod
    def reject_blank_labels(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        for item in values:
            text = str(item).strip()
            if not text:
                raise ValueError("visibility must not contain blank labels")
            cleaned.append(text)
        return cleaned

    @model_validator(mode="after")
    def validity_interval_must_hold(self) -> "AssetGovernance":
        if self.valid_to is not None and self.valid_to <= self.valid_from:
            raise ValueError("valid_to must be later than valid_from")
        if self.published_at.tzinfo is None or self.valid_from.tzinfo is None:
            raise ValueError("published_at and valid_from must be timezone-aware")
        return self

    def is_valid_at(self, moment: datetime) -> bool:
        if moment.tzinfo is None:
            raise ValueError("moment must be timezone-aware")
        if moment < self.valid_from:
            return False
        return self.valid_to is None or moment < self.valid_to

    def is_published(self) -> bool:
        return self.status is AssetStatus.PUBLISHED

    def is_visible_to(self, caller_labels: list[str]) -> bool:
        """Empty `visibility` means public; otherwise require a label overlap."""
        if not self.visibility:
            return True
        return bool(set(self.visibility) & set(caller_labels))


GOVERNANCE_FIELD_MAP: dict[str, str] = {
    "asset_id": "DataAsset.asset_id (AS-<scope>-<type>-<hash8>)",
    "asset_type": "AssetGovernance.asset_type；DataAsset.doc_type 为其落地名",
    "title": "AssetGovernance.title；检索与证据卡片展示名",
    "source_uri": "AssetGovernance.source_uri",
    "source_org": "AssetGovernance.source_org",
    "version": "DataAsset.version（v1/v2…）；知识版本另用 KnowledgeVersion",
    "published_at": "AssetGovernance.published_at",
    "valid_from": "AssetGovernance.valid_from",
    "valid_to": "AssetGovernance.valid_to（None 表示长期有效）",
    "visibility": "AssetGovernance.visibility（空=公开）",
    "content_hash": "AssetGovernance.content_hash ≡ DataAsset.checksum_sha256",
    "status": "AssetGovernance.status（draft/published/deprecated）",
    "locator": "DataAsset.locator（证据定位：asset_id#locator）",
    "modality": "DataAsset.modality（text/table/image/document）",
}


def governance_payload(governance: AssetGovernance) -> dict[str, Any]:
    """JSON-ready dict for manifests and MCP `meta_data`."""
    return governance.model_dump(mode="json")


def split_evidence_ref(ref: str) -> tuple[str, str]:
    """`asset_id#locator` -> (asset_id, locator)."""
    if "#" not in ref:
        raise ValueError(f"evidence ref must be <asset_id>#<locator>: {ref}")
    asset_id, locator = ref.split("#", 1)
    if not asset_id or not locator:
        raise ValueError(f"evidence ref has an empty half: {ref}")
    return asset_id, locator


def blocked_by_visibility(
    refs: Sequence[str],
    caller_labels: Sequence[str],
    visibility: Mapping[str, Sequence[str]],
) -> list[str]:
    """Assets among `refs` the caller may not see.

    One implementation for every gate (MCP tools and Skills) on purpose: the
    permission check is the one place where two copies would drift into a hole.
    Empty `visibility` means public, so an unlabelled caller still sees it.
    """
    labels = set(caller_labels)
    blocked: set[str] = set()
    for ref in refs:
        try:
            asset_id = split_evidence_ref(ref)[0]
        except ValueError:
            asset_id = ref
        allowed = set(visibility.get(asset_id, ()) or ())
        if allowed and not (allowed & labels):
            blocked.add(asset_id)
    return sorted(blocked)
