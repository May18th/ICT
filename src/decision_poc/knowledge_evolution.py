"""Knowledge evolution: proposal -> human decision -> version publish -> rollback.

This is the mechanism behind innovation point 1. The point is *not* "we have a
knowledge graph"; it is that new data produces a **change proposal that already
knows what it will break**, a human decides, and the version can be rolled back.

Design notes:

- A proposal is a diff against the current snapshot, not a re-extraction dump.
  Reviewers see only what changed.
- `ImpactAssessment` is computed before approval, so "改这一条会动到什么" is
  answerable before the change lands.
- `stale_claim_ids` is fed by a caller-supplied `claim_index` (asset_id -> claim
  ids). P3 has no claims yet; P4 fills the index and the same field becomes the
  data-lineage bridge. The hook exists now so the contract never changes.
- Publishing writes JSONL per version; rollback moves the pointer and records the
  action, it does not delete history.
"""

from __future__ import annotations

import json
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from pydantic import Field, model_validator

from .contracts import StrictModel
from .governance import KnowledgeVersion, stable_id
from .ontology import EntityCandidate, OntologyBundle, RelationCandidate, ReviewDecision

SNAPSHOT_HASH_ALGO = "sha256"


class ChangeKind(StrEnum):
    ADD = "add"
    REMOVE = "remove"
    MODIFY = "modify"


class ReviewChoice(StrEnum):
    KEEP = "keep"
    DROP = "drop"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    PUBLISHED = "published"
    REJECTED = "rejected"


class KnowledgeChange(StrictModel):
    change_id: str = Field(min_length=1)
    kind: ChangeKind
    candidate_id: str = Field(min_length=1)
    candidate_kind: str = Field(pattern="^(entity|relation)$")
    label: str = Field(min_length=1)
    entity_types: list[str] = Field(default_factory=list)
    evidence_asset_ids: list[str] = Field(default_factory=list)
    detail: str = ""


class ImpactAssessment(StrictModel):
    affected_names: list[str] = Field(default_factory=list)
    affected_types: list[str] = Field(default_factory=list)
    neighbor_count: int = Field(default=0, ge=0)
    stale_claim_ids: list[str] = Field(default_factory=list)
    risk_flags: list[str] = Field(default_factory=list)
    reason: str = ""

    @model_validator(mode="after")
    def risk_flags_sorted(self) -> "ImpactAssessment":
        self.risk_flags = sorted(set(self.risk_flags))
        return self


class EvolutionProposal(StrictModel):
    proposal_id: str = Field(min_length=1)
    trigger_asset_ids: list[str] = Field(min_length=1)
    base_version: str = Field(min_length=1)
    changes: list[KnowledgeChange] = Field(default_factory=list)
    impact: ImpactAssessment
    status: ProposalStatus = ProposalStatus.PENDING
    note: str = ""

    def summary(self) -> dict[str, int]:
        counts = {kind.value: 0 for kind in ChangeKind}
        for change in self.changes:
            counts[change.kind.value] += 1
        return counts


class VersionHistoryEntry(StrictModel):
    action: str = Field(pattern="^(publish|rollback|reject)$")
    version: str = Field(min_length=1)
    parent_version: str | None = None
    proposal_id: str | None = None
    note: str = ""


class KnowledgeSnapshot(StrictModel):
    version: str = Field(min_length=1, pattern=r"^kb-v\d+\.\d+\.\d+$")
    parent_version: str | None = None
    entities: list[EntityCandidate] = Field(default_factory=list)
    relations: list[RelationCandidate] = Field(default_factory=list)
    source_asset_ids: list[str] = Field(default_factory=list)
    snapshot_hash: str = ""

    def with_hash(self) -> "KnowledgeSnapshot":
        material = json.dumps(
            {
                "version": self.version,
                "entities": [item.model_dump(mode="json") for item in self.entities],
                "relations": [item.model_dump(mode="json") for item in self.relations],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        import hashlib

        return self.model_copy(
            update={"snapshot_hash": hashlib.sha256(material.encode("utf-8")).hexdigest()}
        )


class _Index(StrictModel):
    entities: dict[str, EntityCandidate] = Field(default_factory=dict)
    relations: dict[str, RelationCandidate] = Field(default_factory=dict)


def _index_snapshot(snapshot: KnowledgeSnapshot) -> _Index:
    return _Index(
        entities={item.candidate_id: item for item in snapshot.entities},
        relations={item.candidate_id: item for item in snapshot.relations},
    )


def _index_bundle(bundle: OntologyBundle) -> _Index:
    return _Index(
        entities={item.candidate_id: item for item in bundle.entities},
        relations={item.candidate_id: item for item in bundle.relations},
    )


def _comparable(candidate: EntityCandidate | RelationCandidate) -> dict[str, Any]:
    """Semantic fingerprint used for diffing.

    `asset_id` is intentionally excluded: asset identity is content-addressed
    (`AS-<scope>-<type>-<hash8>`), so editing one cell re-identifies the whole
    file and would mark every candidate from that file as MODIFY. That turns a
    single-line change into a 17-entry proposal, which is worse than useless for
    a reviewer.

    `locator` already encodes logical identity (`scope/doc_type/version#row:N`),
    so a genuine re-version (`v1` -> `v2`) still shows up as a change, while a
    content edit keeps the same locator and only real attribute/quote diffs
    surface. Asset ids stay available on the change record for provenance.
    """
    payload = candidate.model_dump(mode="json")
    payload.pop("decision", None)
    payload["evidence"] = [
        {"locator": item.get("locator", ""), "quote": item.get("quote", "")}
        for item in payload.get("evidence", [])
    ]
    return payload


def _asset_ids(candidate: EntityCandidate | RelationCandidate) -> list[str]:
    return sorted({item.asset_id for item in candidate.evidence})


class KnowledgeRegistry:
    """In-memory version registry with JSONL persistence."""

    def __init__(self, snapshot: KnowledgeSnapshot) -> None:
        self._snapshots: dict[str, KnowledgeSnapshot] = {snapshot.version: snapshot}
        self._current: str = snapshot.version
        self._proposals: dict[str, EvolutionProposal] = {}
        self.history: list[VersionHistoryEntry] = []
        self.last_deferred: list[str] = []

    # --- read -----------------------------------------------------------
    @property
    def current(self) -> KnowledgeSnapshot:
        return self._snapshots[self._current]

    def version(self) -> KnowledgeVersion:
        return KnowledgeVersion(value=self._current)

    def get(self, version: str) -> KnowledgeSnapshot:
        return self._snapshots[version]

    def versions(self) -> list[str]:
        return sorted(self._snapshots, key=lambda value: KnowledgeVersion(value=value).parts)

    def proposal(self, proposal_id: str) -> EvolutionProposal:
        return self._proposals[proposal_id]

    # --- propose --------------------------------------------------------
    def propose(
        self,
        bundle: OntologyBundle,
        *,
        trigger_asset_ids: Sequence[str],
        claim_index: Mapping[str, Sequence[str]] | None = None,
        deprecated_asset_ids: Iterable[str] = (),
        note: str = "",
    ) -> EvolutionProposal:
        triggers = sorted({asset_id for asset_id in trigger_asset_ids if asset_id})
        if not triggers:
            raise ValueError("proposal requires at least one trigger asset")
        old = _index_snapshot(self.current)
        new = _index_bundle(bundle)
        changes: list[KnowledgeChange] = []

        for candidate_id, candidate in new.entities.items():
            previous = old.entities.get(candidate_id)
            if previous is None:
                changes.append(self._change(ChangeKind.ADD, "entity", candidate))
            elif _comparable(previous) != _comparable(candidate):
                changes.append(
                    self._change(ChangeKind.MODIFY, "entity", candidate, detail="属性或置信度变化")
                )
        for candidate_id, previous in old.entities.items():
            if candidate_id in new.entities:
                continue
            if set(_asset_ids(previous)) & set(triggers):
                changes.append(self._change(ChangeKind.REMOVE, "entity", previous, detail="触发资产不再支持该实体"))

        for candidate_id, candidate in new.relations.items():
            previous = old.relations.get(candidate_id)
            if previous is None:
                changes.append(self._change(ChangeKind.ADD, "relation", candidate))
            elif _comparable(previous) != _comparable(candidate):
                changes.append(
                    self._change(ChangeKind.MODIFY, "relation", candidate, detail="属性或置信度变化")
                )
        for candidate_id, previous in old.relations.items():
            if candidate_id in new.relations:
                continue
            if set(_asset_ids(previous)) & set(triggers):
                changes.append(self._change(ChangeKind.REMOVE, "relation", previous, detail="触发资产不再支持该关系"))

        impact = self._assess(changes, new, triggers, claim_index, set(deprecated_asset_ids))
        proposal = EvolutionProposal(
            proposal_id=stable_id("request", "proposal", self._current, ",".join(triggers)),
            trigger_asset_ids=triggers,
            base_version=self._current,
            changes=changes,
            impact=impact,
            note=note,
        )
        self._proposals[proposal.proposal_id] = proposal
        return proposal

    def _change(
        self,
        kind: ChangeKind,
        candidate_kind: str,
        candidate: EntityCandidate | RelationCandidate,
        *,
        detail: str = "",
    ) -> KnowledgeChange:
        if candidate_kind == "entity":
            entity = candidate  # type: ignore[assignment]
            label = f"{entity.type}:{entity.name}"
            types = [entity.type]
        else:
            relation = candidate  # type: ignore[assignment]
            label = f"{relation.subject} -{relation.predicate}-> {relation.object}"
            types = [relation.predicate]
        return KnowledgeChange(
            change_id=stable_id("claim", "change", kind.value, candidate.candidate_id),
            kind=kind,
            candidate_id=candidate.candidate_id,
            candidate_kind=candidate_kind,
            label=label,
            entity_types=types,
            evidence_asset_ids=_asset_ids(candidate),
            detail=detail,
        )

    def _assess(
        self,
        changes: Sequence[KnowledgeChange],
        new: _Index,
        triggers: Sequence[str],
        claim_index: Mapping[str, Sequence[str]] | None,
        deprecated: set[str],
    ) -> ImpactAssessment:
        names: set[str] = set()
        types: set[str] = set()
        flags: set[str] = set()
        for change in changes:
            if change.candidate_kind == "entity":
                names.add(change.label.split(":", 1)[-1])
                types.update(change.entity_types)
                continue
            entity = new.relations.get(change.candidate_id)
            if entity is not None:
                names.update({entity.subject, entity.object})
                types.add(entity.predicate)
                if entity.predicate == "responsible_for":
                    flags.add("responsibility_change")
            if change.kind is ChangeKind.REMOVE:
                flags.add("evidence_removed")
            if set(change.evidence_asset_ids) & deprecated:
                flags.add("deprecated_source")

        # 图邻接 1 跳扩散：语义层查不到邻居就谈不上影响范围
        neighbor_names: set[str] = set()
        for relation in new.relations.values():
            if relation.subject in names or relation.object in names:
                neighbor_names.update({relation.subject, relation.object})
        neighbor_names -= names

        stale: list[str] = []
        if claim_index:
            for asset_id in triggers:
                for claim_id in claim_index.get(asset_id, ()):
                    if claim_id not in stale:
                        stale.append(claim_id)

        return ImpactAssessment(
            affected_names=sorted(names),
            affected_types=sorted(types),
            neighbor_count=len(neighbor_names),
            stale_claim_ids=sorted(stale),
            risk_flags=sorted(flags),
            reason=(
                "无变更" if not changes else f"{len(changes)} 条候选变更，影响 {len(names)} 个实体、{len(neighbor_names)} 个邻接实体"
            ),
        )

    # --- decide ---------------------------------------------------------
    def publish(
        self,
        proposal: EvolutionProposal,
        bundle: OntologyBundle,
        *,
        decisions: Mapping[str, ReviewChoice | str] | None = None,
        note: str = "",
    ) -> KnowledgeSnapshot:
        if proposal.proposal_id not in self._proposals:
            raise KeyError(f"unknown proposal: {proposal.proposal_id}")
        stored = self._proposals[proposal.proposal_id]
        if stored.status is not ProposalStatus.PENDING:
            raise ValueError(f"proposal already {stored.status.value}")
        if proposal.status is not ProposalStatus.PENDING:
            raise ValueError(f"proposal already {proposal.status.value}")
        chosen = decisions or {}

        keep_ids: set[str] = set()
        deferred: list[str] = []
        current = _index_snapshot(self.current)
        new_index = _index_bundle(bundle)

        for change in proposal.changes:
            explicit = chosen.get(change.change_id)
            if explicit is not None:
                value = explicit.value if isinstance(explicit, ReviewChoice) else str(explicit)
                if value == ReviewChoice.KEEP.value:
                    keep_ids.add(change.candidate_id)
                continue

            # 未显式表态时按候选自身的评审结论走，不默认采纳。
            # 过期来源（废止预案）与低置信度候选都是 REVIEW/DROP，默认写入
            # 等于把"待人工确认"当成"已确认"，与安全处理要求相悖。
            candidate = (
                new_index.entities.get(change.candidate_id)
                if change.candidate_kind == "entity"
                else new_index.relations.get(change.candidate_id)
            )
            if change.kind is ChangeKind.REMOVE:
                keep_ids.add(change.candidate_id)  # 源头撤回，默认执行删除
            elif candidate is None:
                keep_ids.add(change.candidate_id)
            elif candidate.decision is ReviewDecision.AUTO_ACCEPT:
                keep_ids.add(change.candidate_id)
            else:
                deferred.append(change.change_id)

        entities: dict[str, EntityCandidate] = dict(current.entities)
        relations: dict[str, RelationCandidate] = dict(current.relations)

        for change in proposal.changes:
            if change.candidate_id not in keep_ids:
                continue
            if change.candidate_kind == "entity":
                candidate = new_index.entities.get(change.candidate_id)
                if candidate is None:
                    entities.pop(change.candidate_id, None)
                else:
                    entities[change.candidate_id] = candidate
            else:
                candidate = new_index.relations.get(change.candidate_id)
                if candidate is None:
                    relations.pop(change.candidate_id, None)
                else:
                    relations[change.candidate_id] = candidate

        version = self._next_version()
        snapshot = KnowledgeSnapshot(
            version=version,
            parent_version=self._current,
            entities=sorted(entities.values(), key=lambda item: item.candidate_id),
            relations=sorted(relations.values(), key=lambda item: item.candidate_id),
            source_asset_ids=sorted(
                {asset_id for item in [*entities.values(), *relations.values()] for asset_id in _asset_ids(item)}
            ),
        ).with_hash()
        self._snapshots[version] = snapshot
        self._current = version
        self.last_deferred = deferred
        self._proposals[proposal.proposal_id] = proposal.model_copy(
            update={"status": ProposalStatus.PUBLISHED}
        )
        self.history.append(
            VersionHistoryEntry(
                action="publish",
                version=version,
                parent_version=snapshot.parent_version,
                proposal_id=proposal.proposal_id,
                note=note,
            )
        )
        return snapshot

    def reject(self, proposal: EvolutionProposal, *, note: str = "") -> EvolutionProposal:
        stored = self._proposals.get(proposal.proposal_id, proposal)
        if stored.status is not ProposalStatus.PENDING:
            raise ValueError(f"proposal already {stored.status.value}")
        updated = proposal.model_copy(update={"status": ProposalStatus.REJECTED, "note": note})
        self._proposals[proposal.proposal_id] = updated
        self.history.append(
            VersionHistoryEntry(
                action="reject",
                version=self._current,
                proposal_id=proposal.proposal_id,
                note=note,
            )
        )
        return updated

    def rollback(self, version: str, *, note: str = "") -> KnowledgeSnapshot:
        if version not in self._snapshots:
            raise KeyError(f"unknown version: {version}")
        self._current = version
        self.history.append(
            VersionHistoryEntry(action="rollback", version=version, note=note)
        )
        return self.current

    def _next_version(self) -> str:
        major, minor, patch = self.version().parts
        return f"kb-v{major}.{minor}.{patch + 1}"

    # --- persistence ----------------------------------------------------
    def save(self, root: Path | str) -> Path:
        base = Path(root)
        for version, snapshot in self._snapshots.items():
            directory = base / version
            directory.mkdir(parents=True, exist_ok=True)
            _write_jsonl(directory / "entities.jsonl", [item.model_dump(mode="json") for item in snapshot.entities])
            _write_jsonl(directory / "relations.jsonl", [item.model_dump(mode="json") for item in snapshot.relations])
            (directory / "snapshot.json").write_text(
                json.dumps(
                    {
                        "version": snapshot.version,
                        "parent_version": snapshot.parent_version,
                        "snapshot_hash": snapshot.snapshot_hash,
                        "source_asset_ids": snapshot.source_asset_ids,
                        "current": version == self._current,
                    },
                    ensure_ascii=False,
                    indent=2,
                )
                + "\n",
                encoding="utf-8",
            )
        (base / "history.jsonl").write_text(
            "\n".join(json.dumps(item.model_dump(mode="json"), ensure_ascii=False) for item in self.history) + "\n",
            encoding="utf-8",
        )
        (base / "registry.json").write_text(
            json.dumps(
                {
                    "current": self._current,
                    "versions": self.versions(),
                    "proposals": [
                        item.model_dump(mode="json") for item in self._proposals.values()
                    ],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return base

    @classmethod
    def load(cls, root: Path | str) -> "KnowledgeRegistry":
        """Rebuild registry from disk so review can happen in a later process.

        Pending proposals must survive: a human confirming a change never
        happens inside the same call that proposed it.
        """
        base = Path(root)
        meta = json.loads((base / "registry.json").read_text(encoding="utf-8"))
        snapshots = {version: load_snapshot(base / version) for version in meta["versions"]}
        current = meta["current"]
        if current not in snapshots:
            raise KeyError(f"current version {current} missing on disk")

        registry = cls(snapshots[current])
        registry._snapshots = snapshots
        registry._current = current
        registry._proposals = {
            item["proposal_id"]: EvolutionProposal.model_validate(item)
            for item in meta.get("proposals", [])
        }
        history_path = base / "history.jsonl"
        if history_path.is_file():
            registry.history = [
                VersionHistoryEntry.model_validate(json.loads(line))
                for line in history_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        return registry


def _write_jsonl(path: Path, rows: Sequence[Mapping[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows) + "\n",
        encoding="utf-8",
    )


def load_snapshot(directory: Path | str) -> KnowledgeSnapshot:
    base = Path(directory)
    meta = json.loads((base / "snapshot.json").read_text(encoding="utf-8"))
    entities = [
        EntityCandidate.model_validate(json.loads(line))
        for line in (base / "entities.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    relations = [
        RelationCandidate.model_validate(json.loads(line))
        for line in (base / "relations.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    return KnowledgeSnapshot(
        version=meta["version"],
        parent_version=meta.get("parent_version"),
        entities=entities,
        relations=relations,
        source_asset_ids=list(meta.get("source_asset_ids", [])),
        snapshot_hash=meta.get("snapshot_hash", ""),
    )


def snapshot_from_bundle(bundle: OntologyBundle, *, version: str = "kb-v1.0.0") -> KnowledgeSnapshot:
    return KnowledgeSnapshot(
        version=version,
        parent_version=None,
        entities=list(bundle.entities),
        relations=list(bundle.relations),
        source_asset_ids=sorted(
            {item.asset_id for candidate in [*bundle.entities, *bundle.relations] for item in candidate.evidence}
        ),
    ).with_hash()


def _trigger_ids(registry: KnowledgeRegistry, bundle: OntologyBundle) -> list[str]:
    """Assets whose content hash is not yet represented in the current snapshot."""
    known = set(registry.current.source_asset_ids)
    seen = {
        item.asset_id
        for candidate in [*bundle.entities, *bundle.relations]
        for item in candidate.evidence
    }
    return sorted(seen - known) or sorted(seen)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="knowledge evolution: propose / confirm / publish / rollback")
    parser.add_argument("action", choices=["init", "propose", "publish", "reject", "rollback", "status"])
    parser.add_argument("--dataset", default=None, help="urban dataset directory")
    parser.add_argument("--out", default="artifacts/kb", help="registry directory")
    parser.add_argument("--proposal", default=None, help="proposal id")
    parser.add_argument("--drop", default="", help="comma separated change ids to reject")
    parser.add_argument("--version", default=None, help="target version for rollback")
    parser.add_argument("--note", default="")
    args = parser.parse_args(argv)

    out = Path(args.out)

    if args.action == "init":
        from .city_extract import build_city_bundle

        snapshot = snapshot_from_bundle(build_city_bundle(args.dataset))
        KnowledgeRegistry(snapshot).save(out)
        print(f"{snapshot.version} entities={len(snapshot.entities)} relations={len(snapshot.relations)} hash={snapshot.snapshot_hash[:12]}")
        return 0

    registry = KnowledgeRegistry.load(out)

    if args.action == "status":
        print(f"current={registry.current.version} hash={registry.current.snapshot_hash[:12]}")
        print(f"versions={registry.versions()}")
        for entry in registry.history:
            print(f"  {entry.action:<8} {entry.version:<10} {entry.proposal_id or '-'}")
        return 0

    if args.action == "propose":
        from .city_extract import build_city_bundle

        bundle = build_city_bundle(args.dataset)
        proposal = registry.propose(bundle, trigger_asset_ids=_trigger_ids(registry, bundle), note=args.note)
        registry.save(out)
        print(f"proposal={proposal.proposal_id} base={proposal.base_version} {proposal.summary()}")
        for change in proposal.changes:
            print(f"  {change.kind.value:<7} {change.candidate_kind:<8} {change.label}")
        print(f"impact flags={proposal.impact.risk_flags} stale_claims={len(proposal.impact.stale_claim_ids)}")
        return 0

    if args.action == "publish":
        from .city_extract import build_city_bundle

        if not args.proposal:
            parser.error("publish requires --proposal")
        drop = {item.strip() for item in args.drop.split(",") if item.strip()}
        decisions = {
            change.change_id: (ReviewChoice.DROP if change.change_id in drop else ReviewChoice.KEEP)
            for change in registry.proposal(args.proposal).changes
        }
        snapshot = registry.publish(
            registry.proposal(args.proposal), build_city_bundle(args.dataset), decisions=decisions, note=args.note
        )
        registry.save(out)
        print(f"published {snapshot.version} parent={snapshot.parent_version} hash={snapshot.snapshot_hash[:12]}")
        if registry.last_deferred:
            print(f"deferred={len(registry.last_deferred)} (未显式确认，按候选评审结论暂不写入)")
        return 0

    if args.action == "reject":
        if not args.proposal:
            parser.error("reject requires --proposal")
        registry.reject(registry.proposal(args.proposal), note=args.note)
        registry.save(out)
        print(f"rejected {args.proposal}")
        return 0

    if args.action == "rollback":
        if not args.version:
            parser.error("rollback requires --version")
        snapshot = registry.rollback(args.version, note=args.note)
        registry.save(out)
        print(f"rolled back to {snapshot.version} hash={snapshot.snapshot_hash[:12]}")
        return 0

    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())


class KnowledgeDiff(StrictModel):
    """Structural diff between two published snapshots.

    `affected_asset_ids` is the join key to provenance: it answers "which
    conclusions rested on knowledge that just moved".
    """

    from_version: str = Field(min_length=1)
    to_version: str = Field(min_length=1)
    from_hash: str
    to_hash: str
    added_entity_ids: list[str] = Field(default_factory=list)
    removed_entity_ids: list[str] = Field(default_factory=list)
    modified_entity_ids: list[str] = Field(default_factory=list)
    added_relation_ids: list[str] = Field(default_factory=list)
    removed_relation_ids: list[str] = Field(default_factory=list)
    modified_relation_ids: list[str] = Field(default_factory=list)
    affected_asset_ids: list[str] = Field(default_factory=list)
    # asset_id 是内容寻址的，跨版本会变；locator 才是稳定血缘键
    affected_locators: list[str] = Field(default_factory=list)

    def summary(self) -> dict[str, int]:
        return {
            "entity_added": len(self.added_entity_ids),
            "entity_removed": len(self.removed_entity_ids),
            "entity_modified": len(self.modified_entity_ids),
            "relation_added": len(self.added_relation_ids),
            "relation_removed": len(self.removed_relation_ids),
            "relation_modified": len(self.modified_relation_ids),
        }

    @property
    def empty(self) -> bool:
        return not any(self.summary().values())


def diff_snapshots(left: KnowledgeSnapshot, right: KnowledgeSnapshot) -> KnowledgeDiff:
    old, new = _index_snapshot(left), _index_snapshot(right)
    diff = KnowledgeDiff(
        from_version=left.version,
        to_version=right.version,
        from_hash=left.snapshot_hash,
        to_hash=right.snapshot_hash,
    )
    for candidate_id, candidate in new.entities.items():
        previous = old.entities.get(candidate_id)
        if previous is None:
            diff.added_entity_ids.append(candidate_id)
        elif _comparable(previous) != _comparable(candidate):
            diff.modified_entity_ids.append(candidate_id)
    diff.removed_entity_ids = sorted(set(old.entities) - set(new.entities))

    for candidate_id, candidate in new.relations.items():
        previous = old.relations.get(candidate_id)
        if previous is None:
            diff.added_relation_ids.append(candidate_id)
        elif _comparable(previous) != _comparable(candidate):
            diff.modified_relation_ids.append(candidate_id)
    diff.removed_relation_ids = sorted(set(old.relations) - set(new.relations))

    touched = {
        cid
        for group in (diff.added_entity_ids, diff.removed_entity_ids, diff.modified_entity_ids)
        for cid in group
    }
    assets: set[str] = set()
    locators: set[str] = set()

    def collect(candidate) -> None:
        if candidate is None:
            return
        assets.update(_asset_ids(candidate))
        locators.update(item.locator for item in candidate.evidence)

    for candidate_id in touched:
        collect(new.entities.get(candidate_id) or old.entities.get(candidate_id))
    for group in (diff.added_relation_ids, diff.removed_relation_ids, diff.modified_relation_ids):
        for candidate_id in group:
            collect(new.relations.get(candidate_id) or old.relations.get(candidate_id))
    diff.affected_asset_ids = sorted(assets)
    diff.affected_locators = sorted(locators)
    diff.added_entity_ids.sort()
    diff.modified_entity_ids.sort()
    diff.added_relation_ids.sort()
    diff.modified_relation_ids.sort()
    return diff


def get_knowledge_diff(
    registry: KnowledgeRegistry, from_version: str, to_version: str
) -> KnowledgeDiff:
    """`get_knowledge_diff` as required by the P4 brief: version-to-version diff."""
    return diff_snapshots(registry.get(from_version), registry.get(to_version))
