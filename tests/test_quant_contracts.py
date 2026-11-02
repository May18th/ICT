"""定量层契约：算不出来要显式声明，不许用近似值冒充结论。"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from decision_poc.quant.contracts import QuantResult, SpreadResult, uncomputable


def _quant(**overrides: object) -> QuantResult:
    data: dict[str, object] = {
        "value": 1.0,
        "unit": "m³/h",
        "formula_id": "Q-TEST",
        "formula_source": "测试来源",
        "assumptions": ["测试假设"],
        "inputs": {"a": 1.0},
        "error_bound": "未率定",
    }
    data.update(overrides)
    return QuantResult(**data)  # type: ignore[arg-type]


def test_computable_result_keeps_value() -> None:
    result = _quant()
    assert result.computable is True
    assert result.value == 1.0


def test_assumptions_cannot_be_empty() -> None:
    with pytest.raises(ValidationError):
        _quant(assumptions=[])


def test_assumptions_cannot_contain_blank_entry() -> None:
    with pytest.raises(ValidationError):
        _quant(assumptions=["有效假设", "   "])


def test_uncomputable_requires_zero_value() -> None:
    with pytest.raises(ValidationError):
        _quant(computable=False, value=3.0, uncomputable_reason="缺少数据")


def test_uncomputable_requires_reason() -> None:
    with pytest.raises(ValidationError):
        _quant(computable=False, value=0.0)


def test_computable_must_not_carry_reason() -> None:
    with pytest.raises(ValidationError):
        _quant(computable=True, uncomputable_reason="不该有原因")


def test_helper_builds_zero_value_with_reason() -> None:
    result = uncomputable(
        formula_id="Q-TEST",
        formula_source="测试来源",
        assumptions=["测试假设"],
        inputs={"a": 1.0},
        reason="缺少管底标高",
    )
    assert result.computable is False
    assert result.value == 0.0
    assert result.uncomputable_reason == "缺少管底标高"


def test_spread_requires_ordered_bounds() -> None:
    with pytest.raises(ValidationError):
        SpreadResult(
            low=3.0,
            nominal=1.0,
            high=2.0,
            unit="m³/h",
            formula_id="Q-TEST",
            formula_source="测试来源",
            assumptions=["测试假设"],
            samples=2,
        )
