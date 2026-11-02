"""不确定性：区间与固定种子抽样，两条都必须可复算。"""

from __future__ import annotations

import pytest

from decision_poc.quant.uncertainty import monte_carlo, range_from_bounds


def _product(point: dict[str, float]) -> float:
    return point["a"] * point["b"]


def test_range_from_bounds_hits_true_extremes() -> None:
    result = range_from_bounds(
        _product,
        {"a": (1.0, 3.0), "b": (2.0, 4.0)},
        formula_id="Q-TEST-PRODUCT",
        unit="m³/h",
    )
    assert result.low == pytest.approx(2.0)
    assert result.nominal == pytest.approx(6.0)
    assert result.high == pytest.approx(12.0)
    assert result.samples == 4


def test_range_from_bounds_is_reproducible() -> None:
    first = range_from_bounds(_product, {"a": (1.0, 3.0), "b": (2.0, 4.0)}, formula_id="Q-T", unit="m³/h")
    second = range_from_bounds(_product, {"a": (1.0, 3.0), "b": (2.0, 4.0)}, formula_id="Q-T", unit="m³/h")
    assert first.model_dump() == second.model_dump()


def test_range_from_bounds_rejects_inverted_bounds() -> None:
    with pytest.raises(ValueError):
        range_from_bounds(_product, {"a": (3.0, 1.0)}, formula_id="Q-T", unit="m³/h")


def test_range_from_bounds_rejects_too_many_dimensions() -> None:
    bounds = {f"x{i}": (1.0, 2.0) for i in range(9)}
    with pytest.raises(ValueError):
        range_from_bounds(lambda point: sum(point.values()), bounds, formula_id="Q-T", unit="m³/h")


def test_monte_carlo_is_deterministic_for_same_seed() -> None:
    bounds = {"a": (1.0, 3.0), "b": (2.0, 4.0)}
    first = monte_carlo(_product, bounds, seed=7, samples=200, formula_id="Q-T", unit="m³/h")
    second = monte_carlo(_product, bounds, seed=7, samples=200, formula_id="Q-T", unit="m³/h")
    assert first.model_dump() == second.model_dump()
    assert first.seed == 7
    assert first.samples == 200


def test_monte_carlo_stays_within_support() -> None:
    bounds = {"a": (1.0, 3.0), "b": (2.0, 4.0)}
    result = monte_carlo(_product, bounds, seed=11, samples=500, formula_id="Q-T", unit="m³/h")
    assert 2.0 <= result.low <= result.nominal <= result.high <= 12.0


def test_monte_carlo_rejects_zero_samples() -> None:
    with pytest.raises(ValueError):
        monte_carlo(_product, {"a": (1.0, 3.0)}, seed=1, samples=0, formula_id="Q-T", unit="m³/h")
