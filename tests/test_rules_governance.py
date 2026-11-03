"""规则治理：observed 定级白名单。

背景：`_response_level` 只接受 `then` 里含 `response_level` 的 observed 规则；
任何非定级规则误写该键都会直接翻转 observed 结论（迁移 2026 规则时已踩过）。
本测试把白名单钉死，新增定级规则必须显式改这里并说明原因。
"""
from pathlib import Path

from decision_poc.deduction import load_rules

RULES = Path(__file__).resolve().parents[1] / "samples" / "rules" / "urban_deduction_rules.json"
ALLOWED = {"RULE-OBS-RESPONSE-YELLOW", "RULE-OBS-RESPONSE-ORANGE"}


def test_observed_response_level_rules_are_whitelisted() -> None:
    rules = load_rules(RULES)
    observed_level = {
        rule.rule_id for rule in rules.rules
        if rule.stage == "observed" and "response_level" in rule.then
    }
    extra = observed_level - ALLOWED
    assert not extra, f"observed 定级规则超出白名单：{sorted(extra)}"
