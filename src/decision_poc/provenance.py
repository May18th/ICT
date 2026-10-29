"""Provenance: claim validation, hash-chained ledger, trace and replay (P4).

The rule that shapes this module: **a claim without locatable evidence is not
recorded at all**. Filtering weak claims downstream would let them reach the
demo answer before anyone notices; refusing them at the door makes "引用完整率"
an assertion (`== 100%`) instead of a statistic.

Chain semantics are deliberately modest: an append-only hash chain, not a
blockchain. `verify()` recounts the chain and reports the first break instead of
raising -- an audit tool that throws away its own findings is not an audit tool.
"""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, Iterable, Mapping, Sequence

from pydantic import Field, field_validator, model_validator

from .contracts import StrictModel
from .governance import ErrorCode, KnowledgeVersion


class ClaimKind(StrEnum):
    FACT = "fact"
    INFERENCE = "inference"
    RULE_OUTPUT = "rule_output"


class ClaimStatus(StrEnum):
    SUPPORTED = "supported"
    UNSUPPORTED = "unsupported"


class UnsupportedReason(StrEnum):
    NO_EVIDENCE = "NO_EVIDENCE"
    UNKNOWN_ASSET = "UNKNOWN_ASSET"
    MALFORMED_REF = "MALFORMED_REF"
    INFERENCE_TOO_FEW_REFS = "INFERENCE_TOO_FEW_REFS"
    INFERENCE_MISSING_PREMISE_REFS = "INFERENCE_MISSING_PREMISE_REFS"
    UNKNOWN_PREMISE = "UNKNOWN_PREMISE"
    UNKNOWN_RULE = "UNKNOWN_RULE"
    RULE_SOURCE_NOT_LOCATABLE = "RULE_SOURCE_NOT_LOCATABLE"


class Claim(StrictModel):
    claim_id: str = Field(min_length=1)
    text: str = Field(min_length=1)
    kind: ClaimKind
    evidence_refs: list[str] = Field(default_factory=list)
    premise_claim_ids: list[str] = Field(default_factory=list)
    produced_by: str = Field(min_length=1)  # module:rule_id / model_id / parser version
    rule_id: str | None = None


class ClaimCheck(StrictModel):
    claim_id: str
    status: ClaimStatus
    reasons: list[str] = Field(default_factory=list)
    resolved_asset_ids: list[str] = Field(default_factory=list)

    @property
    def supported(self) -> bool:
        return self.status is ClaimStatus.SUPPORTED


class RuleRef(StrictModel):
    """Minimal view of a deduction rule, so provenance can verify `rule_output`."""

    rule_id: str = Field(min_length=1)
    source_ref: str = Field(min_length=1)
    version: str = "v1"


class ProvenanceRecord(StrictModel):
    record_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    claim_ids: list[str] = Field(min_length=1)
    asset_fingerprints: dict[str, str] = Field(default_factory=dict)
    knowledge_version: str = Field(pattern=r"^kb-v\d+\.\d+\.\d+$")
    semantic_snapshot_hash: str = Field(min_length=8)
    processors: list[str] = Field(default_factory=list)
    upstream_record_hash: str | None = None
    record_hash: str = Field(default="")

    def computed_hash(self) -> str:
        payload = self.model_dump(mode="json")
        payload.pop("record_hash", None)
        return _hash(payload)


class VerifyReport(StrictModel):
    ok: bool
    checked: int = Field(ge=0)
    first_broken_record_id: str | None = None
    broken_record_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)


class AssetTrace(StrictModel):
    asset_id: str
    locator: str
    fingerprint: str              # 当前账本里的指纹
    recorded_fingerprint: str     # 记录该结论时的指纹
    changed_since_record: bool    # 两者不一致 = 结论建立在旧版本上


class ClaimTrace(StrictModel):
    claim_id: str
    text: str
    kind: ClaimKind
    produced_by: str
    knowledge_version: str
    semantic_snapshot_hash: str
    rule_id: str | None = None
    rule_source_ref: str | None = None
    assets: list[AssetTrace] = Field(default_factory=list)
    record_ids: list[str] = Field(default_factory=list)
    decision_ids: list[str] = Field(default_factory=list)
    premise_claim_ids: list[str] = Field(default_factory=list)


class ReplayBundle(StrictModel):
    """Minimum evidence set needed to re-derive a conclusion."""

    decision_id: str
    claim_ids: list[str]
    asset_fingerprints: dict[str, str]
    knowledge_version: str
    semantic_snapshot_hash: str
    rule_ids: list[str] = Field(default_factory=list)
    processors: list[str] = Field(default_factory=list)
    record_id: str


def _hash(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def split_ref(ref: str) -> tuple[str, str]:
    if "#" not in ref:
        raise ValueError(f"evidence ref must be asset_id#locator, got {ref!r}")
    asset_id, locator = ref.split("#", 1)
    if not asset_id or not locator:
        raise ValueError(f"evidence ref has empty part: {ref!r}")
    return asset_id, locator


class ProvenanceLedger:
    def __init__(
        self,
        *,
        asset_fingerprints: Mapping[str, str],
        knowledge_version: str,
        rules: Mapping[str, RuleRef] | None = None,
    ) -> None:
        KnowledgeVersion(value=knowledge_version)
        self._fingerprints: dict[str, str] = dict(asset_fingerprints)
        self._version = knowledge_version
        self._rules: dict[str, RuleRef] = dict(rules or {})
        self._claims: dict[str, Claim] = {}
        self._checks: dict[str, ClaimCheck] = {}
        self._records: list[ProvenanceRecord] = []

    # --- read ------------------------------------------------------------
    @property
    def knowledge_version(self) -> str:
        return self._version

    def records(self) -> list[ProvenanceRecord]:
        return list(self._records)

    def claim(self, claim_id: str) -> Claim:
        try:
            return self._claims[claim_id]
        except KeyError as exc:
            raise KeyError(f"unknown claim: {claim_id}") from exc

    def check_of(self, claim_id: str) -> ClaimCheck:
        return self._checks[claim_id]

    def citation_completeness(self) -> float:
        """引用完整率 = supported / total；账本只收 supported，所以这里是 1.0 或 0.0 的守门指标。"""
        if not self._checks:
            return 1.0
        supported = sum(1 for check in self._checks.values() if check.supported)
        return supported / len(self._checks)

    # --- validation ------------------------------------------------------
    def register(self, claims: Iterable[Claim]) -> list[ClaimCheck]:
        """Register claims and return one verdict each; nothing is silently dropped."""
        checks: list[ClaimCheck] = []
        for claim in claims:
            self._claims[claim.claim_id] = claim
            check = self._validate(claim)
            self._checks[claim.claim_id] = check
            checks.append(check)
        return checks

    def _validate(self, claim: Claim) -> ClaimCheck:
        reasons: list[str] = []
        assets: list[str] = []

        for ref in claim.evidence_refs:
            try:
                asset_id, _ = split_ref(ref)
            except ValueError:
                reasons.append(f"{UnsupportedReason.MALFORMED_REF.value}:{ref}")
                continue
            if asset_id not in self._fingerprints:
                reasons.append(f"{UnsupportedReason.UNKNOWN_ASSET.value}:{asset_id}")
                continue
            if asset_id not in assets:
                assets.append(asset_id)

        if claim.kind is ClaimKind.FACT:
            if not claim.evidence_refs:
                reasons.append(UnsupportedReason.NO_EVIDENCE.value)

        elif claim.kind is ClaimKind.RULE_OUTPUT:
            if claim.rule_id is None:
                reasons.append(f"{UnsupportedReason.UNKNOWN_RULE.value}:<missing>")
            else:
                rule = self._rules.get(claim.rule_id)
                if rule is None:
                    reasons.append(f"{UnsupportedReason.UNKNOWN_RULE.value}:{claim.rule_id}")
                else:
                    try:
                        rule_asset, _ = split_ref(rule.source_ref)
                    except ValueError:
                        reasons.append(f"{UnsupportedReason.RULE_SOURCE_NOT_LOCATABLE.value}:{claim.rule_id}")
                    else:
                        if rule_asset not in self._fingerprints:
                            reasons.append(
                                f"{UnsupportedReason.RULE_SOURCE_NOT_LOCATABLE.value}:{claim.rule_id}"
                            )
                        elif rule_asset not in assets:
                            assets.append(rule_asset)

        elif claim.kind is ClaimKind.INFERENCE:
            # 必须数"不同出处"：同一 locator 抄两遍不算两条证据
            if len(set(claim.evidence_refs)) < 2:
                reasons.append(UnsupportedReason.INFERENCE_TOO_FEW_REFS.value)
            for premise_id in claim.premise_claim_ids:
                premise = self._claims.get(premise_id)
                if premise is None:
                    reasons.append(f"{UnsupportedReason.UNKNOWN_PREMISE.value}:{premise_id}")
                    continue
                missing = [ref for ref in premise.evidence_refs if ref not in claim.evidence_refs]
                if missing:
                    reasons.append(
                        f"{UnsupportedReason.INFERENCE_MISSING_PREMISE_REFS.value}:{premise_id}:{','.join(missing)}"
                    )

        return ClaimCheck(
            claim_id=claim.claim_id,
            status=ClaimStatus.UNSUPPORTED if reasons else ClaimStatus.SUPPORTED,
            reasons=sorted(reasons),
            resolved_asset_ids=sorted(assets),
        )

    # --- append ----------------------------------------------------------
    def append(
        self,
        *,
        decision_id: str,
        claim_ids: Sequence[str],
        semantic_snapshot_hash: str,
        processors: Sequence[str] = (),
        extra_fingerprints: Mapping[str, str] | None = None,
    ) -> ProvenanceRecord:
        """Chain a decision's claims. Refuses if any claim is unsupported."""
        if not claim_ids:
            raise ValueError("record requires at least one claim")
        unsupported = [
            claim_id
            for claim_id in claim_ids
            if claim_id not in self._checks or not self._checks[claim_id].supported
        ]
        if unsupported:
            raise ValueError(
                f"{ErrorCode.EVIDENCE_INSUFFICIENT.value}: claims not recorded: {unsupported}"
            )

        fingerprints: dict[str, str] = dict(extra_fingerprints or {})
        for claim_id in claim_ids:
            claim = self._claims[claim_id]
            for asset_id in self._checks[claim_id].resolved_asset_ids:
                fingerprints[asset_id] = self._fingerprints[asset_id]

        upstream = self._records[-1].record_hash if self._records else None
        record = ProvenanceRecord(
            record_id=f"PV-{len(self._records) + 1:04d}-{_hash({'d': decision_id, 'c': sorted(claim_ids), 'n': len(self._records)})[:8]}",
            decision_id=decision_id,
            claim_ids=list(claim_ids),
            asset_fingerprints=fingerprints,
            knowledge_version=self._version,
            semantic_snapshot_hash=semantic_snapshot_hash,
            processors=sorted(set(processors)),
            upstream_record_hash=upstream,
        )
        sealed = record.model_copy(update={"record_hash": record.computed_hash()})
        self._records.append(sealed)
        return sealed

    # --- audit -----------------------------------------------------------
    def verify(self) -> VerifyReport:
        broken: list[str] = []
        reasons: list[str] = []
        previous: str | None = None
        for position, record in enumerate(self._records):
            if record.upstream_record_hash != previous:
                broken.append(record.record_id)
                reasons.append(
                    f"CHAIN_BROKEN:{record.record_id}:upstream={record.upstream_record_hash}:expected={previous}"
                )
            recomputed = record.computed_hash()
            if recomputed != record.record_hash:
                broken.append(record.record_id)
                reasons.append(f"HASH_MISMATCH:{record.record_id}:computed={recomputed[:12]}")
            previous = record.record_hash
        ordered = sorted(set(broken), key=broken.index)
        return VerifyReport(
            ok=not ordered,
            checked=len(self._records),
            first_broken_record_id=ordered[0] if ordered else None,
            broken_record_ids=ordered,
            reasons=reasons,
        )

    # --- tracing ---------------------------------------------------------
    def _records_for_claim(self, claim_id: str) -> list[ProvenanceRecord]:
        return [record for record in self._records if claim_id in record.claim_ids]

    def trace_claim(self, claim_id: str) -> ClaimTrace:
        """Walk claim -> evidence assets -> fingerprints -> rules -> ledger records."""
        claim = self.claim(claim_id)
        produced_by = self._records_for_claim(claim_id)
        snapshot = produced_by[0].semantic_snapshot_hash if produced_by else ""
        rule = self._rules.get(claim.rule_id) if claim.rule_id else None
        refs = set(claim.evidence_refs)
        if rule is not None:
            refs.add(rule.source_ref)  # 规则来源也是证据，追溯里必须出现
        assets: list[AssetTrace] = []
        for ref in sorted(refs):
            try:
                asset_id, locator = split_ref(ref)
            except ValueError:
                continue
            current = self._fingerprints.get(asset_id, "")
            recorded = ""
            for record in produced_by:
                if asset_id in record.asset_fingerprints:
                    recorded = record.asset_fingerprints[asset_id]
                    break
            assets.append(
                AssetTrace(
                    asset_id=asset_id,
                    locator=locator,
                    fingerprint=current,
                    recorded_fingerprint=recorded,
                    changed_since_record=bool(recorded) and recorded != current,
                )
            )
        return ClaimTrace(
            claim_id=claim_id,
            text=claim.text,
            kind=claim.kind,
            produced_by=claim.produced_by,
            knowledge_version=self._version,
            semantic_snapshot_hash=snapshot,
            rule_id=claim.rule_id,
            rule_source_ref=rule.source_ref if rule else None,
            assets=assets,
            record_ids=[record.record_id for record in produced_by],
            decision_ids=sorted({record.decision_id for record in produced_by}),
            premise_claim_ids=list(claim.premise_claim_ids),
        )

    def replay(self, decision_id: str) -> ReplayBundle:
        """Minimum evidence set to re-derive one decision."""
        matches = [record for record in self._records if record.decision_id == decision_id]
        if not matches:
            raise KeyError(f"unknown decision: {decision_id}")
        record = matches[-1]
        rule_ids: list[str] = []
        for claim_id in record.claim_ids:
            rule_id = self._claims[claim_id].rule_id
            if rule_id and rule_id not in rule_ids:
                rule_ids.append(rule_id)
        return ReplayBundle(
            decision_id=decision_id,
            claim_ids=list(record.claim_ids),
            asset_fingerprints=dict(record.asset_fingerprints),
            knowledge_version=record.knowledge_version,
            semantic_snapshot_hash=record.semantic_snapshot_hash,
            rule_ids=sorted(rule_ids),
            processors=list(record.processors),
            record_id=record.record_id,
        )

    def affected_decisions(self, changed: Sequence[str]) -> list[str]:
        """Decisions resting on the changed data.

        `changed` accepts asset ids **or** locators. Locators are what survive a
        re-version: asset ids are content-addressed, so a single-cell edit gives
        the whole file a new id and would otherwise look like "nothing affected".

        This is the bridge innovation point 3 needs: 数据变更 -> 受影响结论，
        without re-running the pipeline or guessing.
        """
        changed_set = set(changed)
        affected: set[str] = set()
        for record in self._records:
            if changed_set & set(record.asset_fingerprints):
                affected.add(record.decision_id)
                continue
            for claim_id in record.claim_ids:
                claim = self._claims.get(claim_id)
                if claim is None:
                    continue
                for ref in claim.evidence_refs:
                    try:
                        asset_id, locator = split_ref(ref)
                    except ValueError:
                        continue
                    if asset_id in changed_set or locator in changed_set:
                        affected.add(record.decision_id)
                        break
        return sorted(affected)

    def adopt(self, records: Sequence[ProvenanceRecord], claims: Iterable[Claim] = ()) -> None:
        """Load previously sealed records verbatim.

        Needed because recording and auditing happen in different runs (and, in
        P5, different processes). Seals are not recomputed -- otherwise adopting
        a chain would silently rewrite the very history being audited.
        """
        self.register(list(claims))
        self._records.extend(records)

    def save(self, directory) -> None:
        """Persist records and claims; both are needed to audit in a later process."""
        from pathlib import Path

        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        (target / "records.jsonl").write_text(
            "\n".join(
                json.dumps(record.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
                for record in self._records
            )
            + "\n",
            encoding="utf-8",
        )
        (target / "claims.jsonl").write_text(
            "\n".join(
                json.dumps(claim.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
                for claim in sorted(self._claims.values(), key=lambda item: item.claim_id)
            )
            + "\n",
            encoding="utf-8",
        )

    @classmethod
    def load(
        cls,
        directory,
        *,
        asset_fingerprints: Mapping[str, str],
        knowledge_version: str,
        rules: Mapping[str, RuleRef] | None = None,
    ) -> "ProvenanceLedger":
        """Reload a saved ledger. Seals are adopted verbatim, never recomputed."""
        from pathlib import Path

        base = Path(directory)
        ledger = cls(
            asset_fingerprints=asset_fingerprints, knowledge_version=knowledge_version, rules=rules
        )
        claims_path = base / "claims.jsonl"
        claims = (
            [
                Claim.model_validate(json.loads(line))
                for line in claims_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if claims_path.is_file()
            else []
        )
        records_path = base / "records.jsonl"
        records = (
            [
                ProvenanceRecord.model_validate(json.loads(line))
                for line in records_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            if records_path.is_file()
            else []
        )
        ledger.adopt(records, claims)
        return ledger


def build_ledger(
    dataset_root: str | None = None,
    *,
    knowledge_version: str = "kb-v1.0.0",
    rules: Mapping[str, RuleRef] | None = None,
) -> ProvenanceLedger:
    """Ledger bound to the demo dataset's asset fingerprints."""
    from .urban_dataset import load_urban_assets

    rows = load_urban_assets(dataset_root)
    return ProvenanceLedger(
        asset_fingerprints={asset.asset_id: asset.checksum_sha256 for asset, _ in rows},
        knowledge_version=knowledge_version,
        rules=rules,
    )


__all__ = [
    "AssetTrace",
    "Claim",
    "ClaimCheck",
    "ClaimKind",
    "ClaimStatus",
    "ClaimTrace",
    "ProvenanceLedger",
    "ProvenanceRecord",
    "ReplayBundle",
    "RuleRef",
    "UnsupportedReason",
    "VerifyReport",
    "build_ledger",
    "split_ref",
]
