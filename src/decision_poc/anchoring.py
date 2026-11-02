"""P11 决策锚定包：把一次决策周期压成可复核的承诺元组。

设计来源是"链上只存承诺、原文留在链下"的通用做法，元组形状为：

    L_t = ( H(o_t), H(Z_t), H(a*_t), meta_t )

- `o_t`：结构化观测（事件、时点、语义快照哈希、事件事实）
- `Z_t`：证据包（审计通过的 claim 及其证据定位）
- `a*_t`：选定动作（处置顺序、可发布标志、转人工原因、二次风险）
- `meta_t`：版本与决策标识（**不含墙钟时间**，否则同一决策两次提交哈希不同）

三条边界，写在代码里而不是只写在文档里：

1. **不宣称区块链**。这是 append-only 哈希链审计日志的一次提交，单机、无共识、无多节点；
   对外文案沿用"锚定/哈希链审计日志"，不写"上链""不可篡改存储"（P4 设计红线）。
2. **原文链下**。`save()` 把三个原始包写到 `offchain/<anchor_id>.json`，链上只有三个哈希 +
   元数据；`verify_offchain()` 用链下原文重算哈希并与锚定记录比对，对不上就是 `OFFCHAIN_MISMATCH`。
3. **不抛异常审计**。`verify()` / `verify_offchain()` 与 `ProvenanceLedger.verify()` 同风格：
   报首个断点和全部失配项，而不是遇到第一个问题就抛出去——抛异常会把后面的问题一起藏起来。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from pydantic import Field

from .contracts import StrictModel
from .skills import PipelineReport

ANCHOR_SCHEMA = "p11-anchor-1"


def canonical_hash(payload: Any) -> str:
    """规范化 JSON 的 sha256：排序键 + 紧凑分隔符，跨进程可比。"""
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class ObservationBundle(StrictModel):
    """o_t：决策当时看到的世界。"""

    event_id: str
    as_of: str
    snapshot_hash: str = ""
    facts: dict[str, Any] | None = None


class EvidenceBundle(StrictModel):
    """Z_t：支撑结论的证据包；`text` 需要账本里取，取不到就留空而不是编。"""

    decision_id: str
    claims: list[dict[str, Any]] = Field(default_factory=list)
    citation_completeness: float = 0.0
    rejected: int = 0


class ActionBundle(StrictModel):
    """a*_t：选定的动作与它为什么算"可发布"。"""

    decision_id: str
    action_order: list[str] = Field(default_factory=list)
    publishable: bool = False
    handoff_reasons: list[str] = Field(default_factory=list)
    residual_risks: list[str] = Field(default_factory=list)


class DecisionBundle(StrictModel):
    """链下原文包：三个子包 + 元数据。"""

    observation: ObservationBundle
    evidence: EvidenceBundle
    action: ActionBundle


class AnchorRecord(StrictModel):
    """链上承诺：只有三个哈希 + 元数据 + 链指针。"""

    anchor_id: str
    decision_id: str
    observation_hash: str = Field(min_length=8)
    evidence_hash: str = Field(min_length=8)
    action_hash: str = Field(min_length=8)
    metadata: dict[str, str] = Field(default_factory=dict)
    upstream_anchor_hash: str | None = None
    anchor_hash: str = Field(default="")

    def computed_hash(self) -> str:
        payload = self.model_dump(mode="json")
        payload.pop("anchor_hash", None)
        return canonical_hash(payload)


class AnchorVerify(StrictModel):
    ok: bool = False
    checked: int = 0
    first_broken_anchor_id: str | None = None
    broken_anchor_ids: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(default_factory=list)
    offchain_ok: bool = True
    offchain_mismatches: list[str] = Field(default_factory=list)

    @property
    def tamper_detected(self) -> bool:
        return not self.ok or not self.offchain_ok


def bundle_from_report(
    report: PipelineReport,
    *,
    claim_texts: Mapping[str, str] | None = None,
) -> DecisionBundle:
    """把一次完整决策（含审计）转成链下原文包；缺审计直接拒绝，不生成半套承诺。"""
    audit = report.audit
    if audit is None:
        raise ValueError("anchor requires an audited decision: report.audit is None")
    claims: list[dict[str, Any]] = []
    for claim in audit.claims:
        claims.append(
            {
                "claim_id": claim.claim_id,
                "kind": claim.kind,
                "supported": claim.supported,
                "evidence_refs": list(claim.evidence_refs),
                "rule_id": claim.rule_id,
                "text": (claim_texts or {}).get(claim.claim_id, ""),
            }
        )
    deduction = report.deduction
    return DecisionBundle(
        observation=ObservationBundle(
            event_id=report.event_id,
            as_of=report.as_of.isoformat(),
            snapshot_hash=report.snapshot_hash,
            facts=report.facts.model_dump(mode="json") if report.facts else None,
        ),
        evidence=EvidenceBundle(
            decision_id=audit.decision_id,
            claims=claims,
            citation_completeness=audit.citation_completeness,
            rejected=audit.rejected,
        ),
        action=ActionBundle(
            decision_id=audit.decision_id,
            action_order=list(deduction.action_order) if deduction else [],
            publishable=report.publishable,
            handoff_reasons=list(report.handoff_reasons),
            residual_risks=list(deduction.residual_risks) if deduction else [],
        ),
    )


class AnchorLog:
    """append-only 锚定日志：提交、链校验、链下原文复核、落盘与重载。"""

    def __init__(self) -> None:
        self._records: list[AnchorRecord] = []
        self._bundles: dict[str, DecisionBundle] = {}

    # --- read ------------------------------------------------------------
    def records(self) -> list[AnchorRecord]:
        return list(self._records)

    def bundle(self, anchor_id: str) -> DecisionBundle:
        try:
            return self._bundles[anchor_id].model_copy(deep=True)
        except KeyError as exc:
            raise KeyError(f"unknown anchor: {anchor_id}") from exc

    def anchor_for(self, decision_id: str) -> AnchorRecord:
        for record in reversed(self._records):
            if record.decision_id == decision_id:
                return record
        raise KeyError(f"unknown decision: {decision_id}")

    # --- append ----------------------------------------------------------
    def commit(self, bundle: DecisionBundle) -> AnchorRecord:
        observation_hash = canonical_hash(bundle.observation.model_dump(mode="json"))
        evidence_hash = canonical_hash(bundle.evidence.model_dump(mode="json"))
        action_hash = canonical_hash(bundle.action.model_dump(mode="json"))
        decision_id = bundle.evidence.decision_id
        upstream = self._records[-1].anchor_hash if self._records else None
        anchor_id = f"AN-{len(self._records) + 1:04d}-{canonical_hash({'d': decision_id, 'n': len(self._records)})[:8]}"
        record = AnchorRecord(
            anchor_id=anchor_id,
            decision_id=decision_id,
            observation_hash=observation_hash,
            evidence_hash=evidence_hash,
            action_hash=action_hash,
            metadata={
                # 不放墙钟时间：同一决策重复提交必须得到同一个哈希
                "schema": ANCHOR_SCHEMA,
                "publishable": str(bundle.action.publishable).lower(),
                "claims": str(len(bundle.evidence.claims)),
                "citation_completeness": f"{bundle.evidence.citation_completeness:.4f}",
            },
            upstream_anchor_hash=upstream,
        )
        sealed = record.model_copy(update={"anchor_hash": record.computed_hash()})
        self._records.append(sealed)
        self._bundles[sealed.anchor_id] = bundle
        return sealed

    # --- audit -----------------------------------------------------------
    def verify(self) -> AnchorVerify:
        """重算链：上游指针与自身封印都要对得上。"""
        broken: list[str] = []
        reasons: list[str] = []
        previous: str | None = None
        for record in self._records:
            if record.upstream_anchor_hash != previous:
                broken.append(record.anchor_id)
                reasons.append(
                    f"CHAIN_BROKEN:{record.anchor_id}:"
                    f"upstream={record.upstream_anchor_hash}:expected={previous}"
                )
            recomputed = record.computed_hash()
            if recomputed != record.anchor_hash:
                broken.append(record.anchor_id)
                reasons.append(f"HASH_MISMATCH:{record.anchor_id}:computed={recomputed[:12]}")
            previous = record.anchor_hash
        ordered = sorted(set(broken), key=broken.index)
        return AnchorVerify(
            ok=not ordered,
            checked=len(self._records),
            first_broken_anchor_id=ordered[0] if ordered else None,
            broken_anchor_ids=ordered,
            reasons=reasons,
        )

    def verify_offchain(self) -> AnchorVerify:
        """用链下原文重算三个哈希：链上承诺必须能被复核，否则承诺没有意义。"""
        mismatches: list[str] = []
        reasons: list[str] = []
        for record in self._records:
            bundle = self._bundles.get(record.anchor_id)
            if bundle is None:
                mismatches.append(record.anchor_id)
                reasons.append(f"OFFCHAIN_MISSING:{record.anchor_id}")
                continue
            recomputed = {
                "observation": canonical_hash(bundle.observation.model_dump(mode="json")),
                "evidence": canonical_hash(bundle.evidence.model_dump(mode="json")),
                "action": canonical_hash(bundle.action.model_dump(mode="json")),
            }
            recorded = {
                "observation": record.observation_hash,
                "evidence": record.evidence_hash,
                "action": record.action_hash,
            }
            for field_name, value in recomputed.items():
                if value != recorded[field_name]:
                    mismatches.append(record.anchor_id)
                    reasons.append(
                        f"OFFCHAIN_MISMATCH:{record.anchor_id}:{field_name}:{value[:12]}"
                    )
                    break
        report = self.verify()
        return report.model_copy(
            update={
                "offchain_ok": not mismatches,
                "offchain_mismatches": mismatches,
                "reasons": [*report.reasons, *reasons],
            }
        )

    def prove(self, anchor_id: str) -> dict[str, bool]:
        """逐字段复核一个锚定包：链上承诺 vs 链下原文。"""
        record = next((item for item in self._records if item.anchor_id == anchor_id), None)
        if record is None:
            raise KeyError(f"unknown anchor: {anchor_id}")
        bundle = self.bundle(anchor_id)
        return {
            "observation": canonical_hash(bundle.observation.model_dump(mode="json"))
            == record.observation_hash,
            "evidence": canonical_hash(bundle.evidence.model_dump(mode="json"))
            == record.evidence_hash,
            "action": canonical_hash(bundle.action.model_dump(mode="json")) == record.action_hash,
            "seal": record.computed_hash() == record.anchor_hash,
        }

    # --- 审计演练 --------------------------------------------------------
    def replace_bundle(self, anchor_id: str, bundle: DecisionBundle) -> None:
        """替换链下原文包。只用于演示/测试"原文被改但承诺没变"的检出能力。"""
        if anchor_id not in self._bundles:
            raise KeyError(f"unknown anchor: {anchor_id}")
        self._bundles[anchor_id] = bundle

    # --- persistence -----------------------------------------------------
    def save(self, directory) -> None:
        from pathlib import Path

        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        (target / "anchors.jsonl").write_text(
            "\n".join(
                json.dumps(record.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
                for record in self._records
            )
            + "\n",
            encoding="utf-8",
        )
        offchain = target / "offchain"
        offchain.mkdir(parents=True, exist_ok=True)
        for record in self._records:
            bundle = self._bundles.get(record.anchor_id)
            if bundle is None:
                continue
            (offchain / f"{record.anchor_id}.json").write_text(
                json.dumps(
                    bundle.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, indent=2
                )
                + "\n",
                encoding="utf-8",
            )

    @classmethod
    def load(cls, directory) -> "AnchorLog":
        """重载：锚定记录按封印原样采用，不重算（重算会改写被审计的历史）。"""
        from pathlib import Path

        base = Path(directory)
        log = cls()
        records_path = base / "anchors.jsonl"
        if records_path.is_file():
            log._records = [
                AnchorRecord.model_validate(json.loads(line))
                for line in records_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
        offchain = base / "offchain"
        if offchain.is_dir():
            for path in sorted(offchain.glob("*.json")):
                log._bundles[path.stem] = DecisionBundle.model_validate(
                    json.loads(path.read_text(encoding="utf-8"))
                )
        return log


def anchor_report(
    report: PipelineReport,
    log: AnchorLog | None = None,
) -> tuple[AnchorRecord, AnchorVerify]:
    """便捷入口：给一次审计通过的决策提交锚定包并立刻校验。"""
    log = log or AnchorLog()
    record = log.commit(bundle_from_report(report))
    return record, log.verify_offchain()


__all__ = [
    "ANCHOR_SCHEMA",
    "ActionBundle",
    "AnchorLog",
    "AnchorRecord",
    "AnchorVerify",
    "DecisionBundle",
    "EvidenceBundle",
    "ObservationBundle",
    "anchor_report",
    "bundle_from_report",
    "canonical_hash",
]


# --- CLI ------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")

    from .skills import SkillPipeline, SkillRuntime

    parser = argparse.ArgumentParser(description="anchor one audited decision (hash-chain audit log)")
    parser.add_argument("--event", default="WO-20261029-007")
    parser.add_argument("--as-of", dest="as_of", default="2026-10-29T09:40:00+08:00")
    parser.add_argument("--caller-labels", default="ORG-EMERG")
    parser.add_argument("--out", default=None, help="落盘目录（anchors.jsonl + offchain/）")
    parser.add_argument("--tamper-demo", action="store_true", help="改链下动作包，演示检出")
    parser.add_argument("--json", dest="json_path", default=None)
    args = parser.parse_args(argv)

    runtime = SkillRuntime()
    report = SkillPipeline(runtime).run(
        args.event,
        args.as_of,
        caller_labels=[label for label in args.caller_labels.split(",") if label],
        persist=False,
    )
    ledger = runtime.ensure_ledger()
    texts: dict[str, str] = {}
    if report.audit is not None:
        for claim in report.audit.claims:
            try:
                texts[claim.claim_id] = ledger.claim(claim.claim_id).text
            except KeyError:
                continue
    bundle = bundle_from_report(report, claim_texts=texts)
    log = AnchorLog()
    record = log.commit(bundle)
    verify = log.verify_offchain()

    payload: dict[str, Any] = {
        "anchor_id": record.anchor_id,
        "decision_id": record.decision_id,
        "observation_hash": record.observation_hash[:16],
        "evidence_hash": record.evidence_hash[:16],
        "action_hash": record.action_hash[:16],
        "anchor_hash": record.anchor_hash[:16],
        "claims": len(bundle.evidence.claims),
        "actions": len(bundle.action.action_order),
        "verify_ok": verify.ok,
        "offchain_ok": verify.offchain_ok,
        "tamper_detected": verify.tamper_detected,
    }
    if args.tamper_demo:
        tampered = bundle.model_copy(deep=True)
        tampered.action.action_order = [*tampered.action.action_order, "未获批的高影响动作"]
        log.replace_bundle(record.anchor_id, tampered)
        after = log.verify_offchain()
        payload["tamper_detected"] = after.tamper_detected
        payload["tamper_reasons"] = after.reasons[-2:]
        log.replace_bundle(record.anchor_id, bundle)  # 恢复，落盘的是干净版本

    if args.out:
        log.save(args.out)
        payload["saved"] = args.out
    if args.json_path:
        target = Path(args.json_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )

    print(
        f"锚定 {record.anchor_id} 决策 {record.decision_id}；"
        f"链校验={'通过' if verify.ok else '失败'}，"
        f"链下复核={'通过' if verify.offchain_ok else '失败'}"
    )
    if args.tamper_demo:
        print(f"篡改演练：检出={payload['tamper_detected']} 证据={payload.get('tamper_reasons')}")
    return 0 if verify.ok and verify.offchain_ok else 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
