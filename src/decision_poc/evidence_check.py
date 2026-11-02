"""对外数字自检：一条命令核对全部宣称证据，漂移即失败。

为什么需要它
------------
答辩材料里的每个数字都散在报告、面板、README 里。只要有人改了规则、样本或阈值，
数字就会变，而旧文案还停在原处——评委按 README 复现出来对不上，整条证据链就会被质疑。

所以把"对外宣称的数字"冻结成 `samples/eval/evidence_expectations.json`，本模块负责：

1. **重跑**：六跳推演、P8 评测、P9 双会话进化、MCP 工具数、测试数口径；
2. **逐项比对**：期望 vs 实际，浮点允许 1e-6，其余精确；
3. **漂移即失败**：任一项不一致，退出码 1，并列出"哪一项、期望多少、实际多少"。

三条口径：

- 期望值是有语义的（六跳顺序、C1/C2/C3、缺口 1 台、偏差 0.2264→0、评审 0.1171→0），
  不是把当前输出原样抄一遍；改数字必须改期望文件并说明原因。
- 自检不写任何文件、不落账（`persist=False`），跑一百遍不污染工作区。
- 自检本身确定性：两次 `run_showcase()` 结果逐位一致记为一项检查。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from pydantic import Field

from .contracts import StrictModel

REPO_ROOT = Path(__file__).resolve().parents[2]
EXPECTATIONS_PATH = REPO_ROOT / "samples" / "eval" / "evidence_expectations.json"
# 测试数同样是对外数字：文档里写了几种，根 pytest 真跑出来是几种，必须是同一个值。
# 实测值由 tests/test_test_count_guard.py 用 `request.session.testscollected` 核对
# （evidence_check 自己不能递归跑 pytest），这里只核对"文档之间没有漂"。
DOCS_WITH_TEST_COUNT = (
    "README.md",
    "docs/competition-gap-audit.md",
    "docs/competition/preliminary-submission-draft.md",
)
TEST_COUNT_PATTERNS = (
    re.compile(r"(\d+)\s*项(?:测试)?通过"),
    re.compile(r"#\s*(\d+)\s*passed"),
)
SCHEMA_VERSION = "evidence-check-1"
EPOCH = "2026-10-29T09:40:00+08:00"
EVENT_ID = "WO-20261029-007"
FLOAT_TOLERANCE = 1e-6


class EvidenceItem(StrictModel):
    path: str
    expected: Any
    actual: Any
    passed: bool
    note: str = ""


class EvidenceReport(StrictModel):
    schema_version: str = SCHEMA_VERSION
    expectations_digest: str = ""
    total: int = 0
    passed_count: int = 0
    failed_count: int = 0
    passed: bool = False
    items: list[EvidenceItem] = Field(default_factory=list)

    @property
    def failures(self) -> list[EvidenceItem]:
        return [item for item in self.items if not item.passed]


# --- 实际值采集 -----------------------------------------------------------


def _deduction_actuals() -> dict[str, Any]:
    from .deduction import DEDUCTION_STAGES, DeductionEngine

    engine = DeductionEngine.load()
    result = engine.deduce(EVENT_ID, as_of=EPOCH)
    stages: list[str] = []
    for step in result.steps:
        if step.stage in DEDUCTION_STAGES and step.stage not in stages:
            stages.append(step.stage)
    actions = result.action_order
    isolate = next((index for index, text in enumerate(actions) if "隔离风险源" in text), None)
    drain = next((index for index, text in enumerate(actions) if "抽排" in text), None)
    return {
        "event_id": result.event_id,
        "six_hops": stages,
        "conflict_ids": [item.conflict_id for item in result.conflicts],
        "conflict_kinds": sorted({item.kind.value for item in result.conflicts}),
        "resource_gap": sum(item.gap for item in result.resource_gaps),
        "action_count": len(actions),
        "isolate_before_drain": bool(
            isolate is not None and drain is not None and isolate < drain
        ),
        "handoff_required": result.handoff_required,
        "snapshot_hash_len": len(result.snapshot_hash),
    }


def _mcp_actuals() -> dict[str, Any]:
    from .mcp_server import UrbanToolkit

    toolkit = UrbanToolkit()
    names = [tool["name"] for tool in toolkit.tools()]
    return {"tools_count": len(names), "has_allocation_plan": "allocation_plan" in names}


def _p8_actuals() -> dict[str, Any]:
    from .urban_eval import evaluate, load_cases

    report = evaluate(load_cases())
    metrics = report.metrics
    return {
        "cases_total": metrics.cases_total,
        "cases_passed": metrics.cases_passed,
        "cases_failed": metrics.cases_failed,
        "assets_parsed": metrics.assets_parsed,
        "assets_total": metrics.assets_total,
        "unmapped_columns": metrics.unmapped_columns,
        "locator_hit_rate": metrics.locator_hit_rate,
        "six_hop_rate": metrics.six_hop_rate,
        "citation_completeness": metrics.citation_completeness,
        "unsupported_claims": metrics.unsupported_claims,
        "safety_by_kind": dict(metrics.safety_by_kind),
    }


def _p9_actuals() -> dict[str, Any]:
    from .allocation import cases_digest, load_cases
    from .policy_evolution import run_showcase

    showcase = run_showcase()
    first, second = showcase.first, showcase.second
    repeat = run_showcase()
    deterministic = repeat.model_dump(mode="json") == showcase.model_dump(mode="json")
    warm = second.warm_start
    baseline = first.real_event_baseline
    evolved = first.real_event_evolved
    return {
        "cases_digest": cases_digest(load_cases()),
        "feedback_verified": first.feedback_verified,
        "train": {
            "initial_deviation": first.initial_train.deviation,
            "final_deviation": first.final_train.deviation,
            "initial_l1_units": first.initial_train.allocation_l1_units,
            "final_l1_units": first.final_train.allocation_l1_units,
            "initial_critical_unmet_units": first.initial_train.critical_unmet_units,
            "final_critical_unmet_units": first.final_train.critical_unmet_units,
            "initial_missed_transfers": first.initial_train.missed_transfers,
            "final_missed_transfers": first.final_train.missed_transfers,
            "initial_order_hits": first.initial_train.order_hits,
            "final_order_hits": first.final_train.order_hits,
        },
        "holdout": {
            "initial_deviation": first.initial_holdout.deviation,
            "final_deviation": first.final_holdout.deviation,
        },
        "rounds_actions": [item.action for item in first.rounds],
        "releases": [item.label for item in first.releases],
        "final_label": first.final_label,
        "review": {
            "review_id": first.reviews[0].review_id if first.reviews else "",
            "deviation_before": first.reviews[0].deviation_before if first.reviews else 0.0,
            "deviation_after": first.reviews[0].deviation_after if first.reviews else 0.0,
            "expected_allocation": dict(first.reviews[0].expected_allocation) if first.reviews else {},
        },
        "warm_start": {
            "entry_id": warm.entry_id if warm is not None else "",
            "label": warm.label if warm is not None else "",
            "similarity": warm.similarity if warm is not None else 0.0,
            "second_initial_deviation": second.initial_train.deviation,
            "second_rounds": len(second.rounds),
            "second_promoted_rounds": second.promoted_rounds,
        },
        "memory_entries": len(showcase.memory_entries),
        "real_event": {
            "baseline_allocation": dict(baseline.allocation_by_district) if baseline else {},
            "evolved_allocation": dict(evolved.allocation_by_district) if evolved else {},
            "evolved_unmet": dict(evolved.unmet_by_district) if evolved else {},
            "evolved_transfer_count": len(evolved.transfers) if evolved else 0,
        },
        "deterministic": deterministic,
    }


def _p11_actuals() -> dict[str, Any]:
    """P11 消融阶梯：每档同口径指标的冻结值 + 确定性。"""
    from .ablation import FROZEN_METRICS, evaluate

    report = evaluate()
    again = evaluate()
    return {
        "configs": [item.config for item in report.configs],
        "metrics": {
            item.config: {name: item.metrics[name] for name in FROZEN_METRICS}
            for item in report.configs
        },
        "pairwise_pairs": len(report.pairwise),
        "deterministic": again.fingerprint() == report.fingerprint(),
    }


def _p12_actuals() -> dict[str, Any]:
    """P11 锚定包：承诺可复核、篡改可检出、同一决策同哈希。"""
    from .anchoring import AnchorLog, bundle_from_report
    from .skills import SkillPipeline, SkillRuntime

    runtime = SkillRuntime()
    report = SkillPipeline(runtime).run(EVENT_ID, EPOCH, caller_labels=["ORG-EMERG"], persist=False)
    bundle = bundle_from_report(report)
    log = AnchorLog()
    record = log.commit(bundle)
    verify = log.verify_offchain()

    tampered = bundle.model_copy(deep=True)
    tampered.action.action_order = [*tampered.action.action_order, "未获批的高影响动作"]
    log.replace_bundle(record.anchor_id, tampered)
    tamper = log.verify_offchain()

    again = bundle_from_report(report)
    return {
        "anchors": verify.checked,
        "verify_ok": verify.ok,
        "offchain_ok": verify.offchain_ok,
        "tamper_detected": tamper.tamper_detected,
        "deterministic": AnchorLog().commit(again).anchor_hash == record.anchor_hash,
    }


def _test_suite_actuals() -> dict[str, Any]:
    """文档里对测试数的声明。不同文档写出不同数字即视为漂移。"""
    return {"docs_declared": docs_declared_test_counts()}


SKILL_EVIDENCE_PATH = REPO_ROOT / "samples" / "nexent" / "live" / "skills.json"
SKILL_SOURCE_GLOB = "samples/nexent/skill/*/SKILL.md"
PLATFORM_UI_PATH = REPO_ROOT / "docs" / "reports" / "captures" / "nexent" / "platform_ui.json"


def _sha256_file(path: Path) -> str:
    import hashlib

    return hashlib.sha256(path.read_bytes()).hexdigest()


def _platform_skills_actuals() -> dict[str, Any]:
    """平台技能仓库的固化证据 vs 仓库里的 SKILL.md。

    这层只做**离线可核**的部分：证据文件里记了上传时 SKILL.md 的 file_sha256 与
    平台存储正文的 content_sha256，所以一旦有人改了 SKILL.md 却没重新上传，
    这里的 `local_file_hashes_match` 会变 False——"平台上有技能"这句话就不能再照抄。
    平台侧是否仍然一致需要重跑 `deploy/nexent/upload_skills.py`（它自己会逐位复核）。
    """
    if not SKILL_EVIDENCE_PATH.is_file():
        return {"captured": False}
    payload = json.loads(SKILL_EVIDENCE_PATH.read_text(encoding="utf-8"))
    skills = payload.get("skills") or []
    recorded = {item.get("name"): item for item in skills if isinstance(item, dict)}
    local = {}
    for path in sorted(REPO_ROOT.glob(SKILL_SOURCE_GLOB)):
        local[path.parent.name] = _sha256_file(path)
    hashes_match = all(
        local.get(name) is not None and local[name] == record.get("file_sha256")
        for name, record in recorded.items()
    )
    verification = payload.get("platform_verification") or []
    return {
        "captured": True,
        "skill_count": len(recorded),
        "names": sorted(name for name in recorded if name),
        "local_file_hashes_match": bool(recorded) and hashes_match and set(local) == set(recorded),
        "platform_content_matches": bool(verification)
        and all(item.get("content_matches") for item in verification),
    }


def _platform_ui_actuals() -> dict[str, Any]:
    """平台界面截图证据：页面是否全部命中期望文字，图纸哈希是否还对得上。

    截图属于对外材料，被替换或重新截过都必须重新生成证据；这里用 sha256 把
    "证据文件里记的那张图"和"仓库里现在那张图"绑在一起。
    """
    if not PLATFORM_UI_PATH.is_file():
        return {"captured": False}
    payload = json.loads(PLATFORM_UI_PATH.read_text(encoding="utf-8"))
    pages = [page for page in payload.get("pages") or [] if isinstance(page, dict)]
    hashes_ok = True
    for page in pages:
        shot = page.get("screenshot")
        if not shot:
            hashes_ok = False
            continue
        path = REPO_ROOT / shot
        if not path.is_file() or _sha256_file(path) != page.get("screenshot_sha256"):
            hashes_ok = False
    return {
        "captured": bool(pages),
        "page_count": len(pages),
        "all_expectations_met": bool(payload.get("all_expectations_met")),
        "screenshot_hashes_ok": hashes_ok and bool(pages),
    }


def docs_declared_test_counts() -> list[int]:
    """扫一遍对外文档宣称的"测试通过数"，去重排序返回。"""
    counts: set[int] = set()
    for rel in DOCS_WITH_TEST_COUNT:
        text = (REPO_ROOT / rel).read_text(encoding="utf-8")
        for pattern in TEST_COUNT_PATTERNS:
            counts.update(int(match.group(1)) for match in pattern.finditer(text))
    return sorted(counts)


def collect_actuals() -> dict[str, Any]:
    """重跑全部对外宣称的数字；不写文件、不落账。"""
    return {
        "p6_deduction": _deduction_actuals(),
        "mcp": _mcp_actuals(),
        "p8_eval": _p8_actuals(),
        "p9_evolution": _p9_actuals(),
        "p11_ablation": _p11_actuals(),
        "p12_anchoring": _p12_actuals(),
        "test_suite": _test_suite_actuals(),
        "platform_skills": _platform_skills_actuals(),
        "platform_ui": _platform_ui_actuals(),
    }


# --- 比对 ----------------------------------------------------------------


def _compare(expected: Any, actual: Any, path: str) -> list[EvidenceItem]:
    items: list[EvidenceItem] = []
    if isinstance(expected, Mapping):
        if not isinstance(actual, Mapping):
            return [EvidenceItem(path=path, expected=expected, actual=actual, passed=False, note="类型不符")]
        for key, value in expected.items():
            items.extend(_compare(value, actual.get(key, _MISSING), f"{path}.{key}" if path else key))
        return items
    if isinstance(expected, list):
        passed = isinstance(actual, list) and actual == expected
        return [
            EvidenceItem(
                path=path,
                expected=expected,
                actual=actual,
                passed=bool(passed),
                note="" if passed else "列表顺序或内容不一致",
            )
        ]
    if isinstance(expected, float) and isinstance(actual, (int, float)) and not isinstance(actual, bool):
        passed = abs(float(actual) - expected) <= FLOAT_TOLERANCE
        return [EvidenceItem(path=path, expected=expected, actual=actual, passed=passed)]
    passed = actual == expected
    return [EvidenceItem(path=path, expected=expected, actual=actual, passed=passed)]


class _Missing:
    def __repr__(self) -> str:  # pragma: no cover - only for messages
        return "<缺失>"


_MISSING = _Missing()


def load_expectations(path: Path | str = EXPECTATIONS_PATH) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def expectations_digest(payload: Mapping[str, Any]) -> str:
    import hashlib

    encoded = json.dumps(
        {"schema_version": payload.get("schema_version"), "checks": payload.get("checks", {})},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def run_evidence_check(
    expectations: Mapping[str, Any] | None = None,
    *,
    expectations_path: Path | str = EXPECTATIONS_PATH,
    actuals: Mapping[str, Any] | None = None,
) -> EvidenceReport:
    payload = dict(expectations) if expectations is not None else load_expectations(expectations_path)
    checks = payload.get("checks", {})
    values = dict(actuals) if actuals is not None else collect_actuals()
    items: list[EvidenceItem] = []
    for section, expected in checks.items():
        items.extend(_compare(expected, values.get(section, _MISSING), section))
    passed = sum(1 for item in items if item.passed)
    return EvidenceReport(
        expectations_digest=expectations_digest(payload),
        total=len(items),
        passed_count=passed,
        failed_count=len(items) - passed,
        passed=passed == len(items),
        items=items,
    )


# --- 输出 ----------------------------------------------------------------


def to_markdown(report: EvidenceReport) -> str:
    lines = [
        "# 对外数字自检报告",
        "",
        "由 `python -m decision_poc.evidence_check --report <path>` 生成，**不要手改**。",
        "",
        f"- 期望指纹：`{report.expectations_digest[:16]}`（期望文件被改动即换指纹）",
        f"- 检查项：{report.total} 项，通过 {report.passed_count}，失败 {report.failed_count}",
        f"- 结论：{'全部一致' if report.passed else '存在漂移，禁止按旧文案答辩'}",
        "",
        "| 检查项 | 期望 | 实际 | 结果 |",
        "| --- | --- | --- | --- |",
    ]
    for item in report.items:
        expected = json.dumps(item.expected, ensure_ascii=False)
        actual = json.dumps(item.actual, ensure_ascii=False, default=str)
        mark = "通过" if item.passed else "**失败**"
        lines.append(f"| `{item.path}` | `{expected}` | `{actual}` | {mark} |")
    if report.failures:
        lines += ["", "## 失败明细", ""]
        for item in report.failures:
            lines.append(f"- `{item.path}`：期望 `{item.expected}`，实际 `{item.actual}`")
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    import sys

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    import argparse

    parser = argparse.ArgumentParser(description="verify every publicly quoted number against expectations")
    parser.add_argument("--expectations", default=str(EXPECTATIONS_PATH))
    parser.add_argument("--json", dest="json_path", default=None, help="写出机器可读结果")
    parser.add_argument("--report", dest="report_path", default=None, help="写出 Markdown 报告")
    parser.add_argument("--dump", action="store_true", help="只打印实际值（用于更新期望文件）")
    args = parser.parse_args(argv)

    if args.dump:
        print(json.dumps(collect_actuals(), ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    report = run_evidence_check(expectations_path=args.expectations)
    for item in report.failures:
        print(f"  ! {item.path}: 期望 {item.expected} 实际 {item.actual}")
    print(
        f"对外数字自检：{report.passed_count}/{report.total} 项一致"
        f"（期望指纹 {report.expectations_digest[:16]}）"
    )
    if args.json_path:
        Path(args.json_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.json_path).write_text(report.model_dump_json(indent=2) + "\n", encoding="utf-8")
        print(f"已写出 {args.json_path}")
    if args.report_path:
        Path(args.report_path).parent.mkdir(parents=True, exist_ok=True)
        Path(args.report_path).write_text(to_markdown(report), encoding="utf-8")
        print(f"已写出 {args.report_path}")
    return 0 if report.passed else 1


__all__ = [
    "EvidenceItem",
    "EvidenceReport",
    "EXPECTATIONS_PATH",
    "collect_actuals",
    "expectations_digest",
    "load_expectations",
    "run_evidence_check",
    "to_markdown",
]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
