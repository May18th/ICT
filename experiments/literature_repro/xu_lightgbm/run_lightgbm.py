"""Xu 2023 LightGBM 方法基线。

论文口径：49 个情景、10 个积水点、11 个输入变量、5 个代表情景测试。
默认只生成合成 fixture；没有论文原始数据时不允许把输出标成论文结果。
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path

import numpy as np


FEATURES = (
    "rain_total_mm",
    "rain_return_period_y",
    "rain_peak_mm_h",
    "rain_max_2h_mm",
    "rain_max_3h_mm",
    "rain_prepeak_mm",
    "tide_max_m",
    "tide_return_period_y",
    "tide_mean_m",
    "tide_max_5h_mean_m",
    "point_location",
)
TARGET = "target_max_depth_cm"
N_SCENARIOS = 49
N_POINTS = 10
N_TEST_SCENARIOS = 5
PAPER_CONFIG = {"learning_rate": 0.11, "n_estimators": 450, "num_leaves": 12}
GRID = (
    PAPER_CONFIG,
    {"learning_rate": 0.05, "n_estimators": 250, "num_leaves": 31},
    {"learning_rate": 0.10, "n_estimators": 350, "num_leaves": 16},
)


def _synthetic_rows(seed: int = 20261122) -> list[dict[str, object]]:
    """生成 49×10 的 paper-like shape；数值单位仅用于方法 smoke。"""

    rng = np.random.default_rng(seed)
    rows: list[dict[str, object]] = []
    for scenario_idx in range(N_SCENARIOS):
        total = float(rng.uniform(45, 260))
        peak = float(rng.uniform(20, 105))
        max_2h = float(peak * rng.uniform(1.1, 2.0))
        max_3h = float(max_2h * rng.uniform(1.05, 1.8))
        prepeak = float(total * rng.uniform(0.2, 0.8))
        rain_return = float(rng.uniform(1.5, 50))
        tide_max = float(rng.uniform(0.2, 2.4))
        tide_return = float(rng.uniform(1.2, 20))
        tide_mean = float(tide_max * rng.uniform(0.45, 0.9))
        tide_5h = float(tide_mean * rng.uniform(0.9, 1.2))
        for point_idx in range(N_POINTS):
            location = point_idx + 1
            pressure = (
                0.11 * total
                + 0.08 * peak
                + 0.05 * max_2h
                + 0.04 * max_3h
                + 0.06 * prepeak
                + 1.7 * rain_return**0.35
                + 9.0 * tide_max
                + 1.6 * tide_return**0.25
                + 5.0 * tide_mean
                + 2.5 * tide_5h
                + 1.2 * location
                + rng.normal(0, 0.01)
            )
            rows.append(
                {
                    "scenario_id": f"S{scenario_idx + 1:02d}",
                    "point_id": f"P{location:02d}",
                    "rain_total_mm": total,
                    "rain_return_period_y": rain_return,
                    "rain_peak_mm_h": peak,
                    "rain_max_2h_mm": max_2h,
                    "rain_max_3h_mm": max_3h,
                    "rain_prepeak_mm": prepeak,
                    "tide_max_m": tide_max,
                    "tide_return_period_y": tide_return,
                    "tide_mean_m": tide_mean,
                    "tide_max_5h_mean_m": tide_5h,
                    "point_location": float(location),
                    TARGET: float(max(0.0, pressure)),
                }
            )
    return rows


def _read_csv(path: Path) -> list[dict[str, object]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        required = {"scenario_id", "point_id", *FEATURES, TARGET}
        missing = sorted(required - set(reader.fieldnames or []))
        if missing:
            raise ValueError(f"输入缺少字段: {', '.join(missing)}")
        rows: list[dict[str, object]] = []
        for line_no, raw in enumerate(reader, start=2):
            try:
                row: dict[str, object] = {
                    "scenario_id": raw["scenario_id"],
                    "point_id": raw["point_id"],
                }
                row.update({name: float(raw[name]) for name in FEATURES})
                row[TARGET] = float(raw[TARGET])
            except (TypeError, ValueError) as exc:
                raise ValueError(f"第 {line_no} 行存在非数值字段: {exc}") from exc
            rows.append(row)
    if not rows:
        raise ValueError("输入 CSV 没有数据行")
    return rows


def _validate_shape(rows: list[dict[str, object]]) -> tuple[list[str], list[str]]:
    scenarios = sorted({str(row["scenario_id"]) for row in rows})
    points = sorted({str(row["point_id"]) for row in rows})
    if len(scenarios) != N_SCENARIOS or len(points) != N_POINTS or len(rows) != N_SCENARIOS * N_POINTS:
        raise ValueError(
            f"需要严格的 {N_SCENARIOS}×{N_POINTS} 样本形状，实际为 "
            f"{len(scenarios)} 个情景、{len(points)} 个点、{len(rows)} 行"
        )
    keys = [(str(row["scenario_id"]), str(row["point_id"])) for row in rows]
    if len(set(keys)) != len(keys):
        raise ValueError("scenario_id + point_id 存在重复行")
    counts = {scenario: sum(1 for row in rows if str(row["scenario_id"]) == scenario) for scenario in scenarios}
    bad = {scenario: count for scenario, count in counts.items() if count != N_POINTS}
    if bad:
        raise ValueError(f"每个情景必须恰有 10 个点，异常情景: {bad}")
    return scenarios, points


def _zscore(train: np.ndarray, values: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    mean = train.mean(axis=0)
    scale = train.std(axis=0)
    scale[scale == 0] = 1.0
    return (train - mean) / scale, (values - mean) / scale, np.stack([mean, scale])


def _metrics(y_true: np.ndarray, y_pred: np.ndarray) -> dict[str, float]:
    residual = y_true - y_pred
    denom = float(np.sum((y_true - np.mean(y_true)) ** 2))
    nonzero = np.abs(y_true) > 1e-9
    mape = float(np.mean(np.abs(residual[nonzero] / y_true[nonzero])) * 100) if np.any(nonzero) else float("nan")
    return {
        "rmse": float(np.sqrt(np.mean(residual**2))),
        "mape_pct": mape,
        "sse": float(np.sum(residual**2)),
        "nse": 1.0 - float(np.sum(residual**2) / denom) if denom else float("nan"),
    }


def _folds(scenarios: list[str], folds: int = 10) -> list[set[str]]:
    return [set(scenarios[index::folds]) for index in range(folds)]


def run(rows: list[dict[str, object]], source_type: str, output_dir: Path) -> dict[str, object]:
    try:
        from lightgbm import LGBMRegressor
    except ImportError as exc:
        raise RuntimeError("缺少 LightGBM；不使用其他模型替代。") from exc

    scenarios, points = _validate_shape(rows)
    test_scenarios = set(scenarios[:N_TEST_SCENARIOS])
    train_scenarios = [scenario for scenario in scenarios if scenario not in test_scenarios]
    train_rows = [row for row in rows if str(row["scenario_id"]) in train_scenarios]
    test_rows = [row for row in rows if str(row["scenario_id"]) in test_scenarios]
    x_train_raw = np.asarray([[float(row[name]) for name in FEATURES] for row in train_rows])
    y_train = np.asarray([float(row[TARGET]) for row in train_rows])
    x_test_raw = np.asarray([[float(row[name]) for name in FEATURES] for row in test_rows])
    y_test = np.asarray([float(row[TARGET]) for row in test_rows])
    x_train, x_test, zscore_params = _zscore(x_train_raw, x_test_raw)
    train_scenario_rows = np.asarray([str(row["scenario_id"]) for row in train_rows], dtype=object)
    fold_sets = _folds(train_scenarios)

    cv_results: list[dict[str, object]] = []
    for config in GRID:
        fold_scores: list[float] = []
        for fold_test_scenarios in fold_sets:
            valid_mask = np.isin(train_scenario_rows, list(fold_test_scenarios))
            fit_mask = ~valid_mask
            model = LGBMRegressor(objective="regression", random_state=42, verbosity=-1, **config)
            model.fit(x_train[fit_mask], y_train[fit_mask])
            fold_scores.append(_metrics(y_train[valid_mask], model.predict(x_train[valid_mask]))["rmse"])
        cv_results.append({"config": config, "cv_rmse_mean": float(np.mean(fold_scores)), "cv_rmse_folds": fold_scores})
    best = min(cv_results, key=lambda item: float(item["cv_rmse_mean"]))
    best_config = dict(best["config"])
    final_model = LGBMRegressor(objective="regression", random_state=42, verbosity=-1, **best_config)
    final_model.fit(x_train, y_train)
    predictions = final_model.predict(x_test)
    metrics = _metrics(y_test, predictions)
    per_point: dict[str, dict[str, float]] = {}
    for point_id in points:
        mask = np.asarray([str(row["point_id"]) == point_id for row in test_rows])
        per_point[point_id] = _metrics(y_test[mask], predictions[mask])

    canonical = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    predictions_rows = [
        {"scenario_id": str(row["scenario_id"]), "point_id": str(row["point_id"]), "y_true": float(true), "y_pred": float(pred)}
        for row, true, pred in zip(test_rows, y_test, predictions)
    ]
    predictions_path = output_dir / "predictions.json"
    output_dir.mkdir(parents=True, exist_ok=True)
    predictions_path.write_text(json.dumps(predictions_rows, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    result = {
        "source_type": source_type,
        "paper_result_claim": False,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "command": " ".join(sys.argv),
        "environment": {"python": sys.version.split()[0], "platform": platform.platform(), "numpy": np.__version__},
        "input_sha256": hashlib.sha256(canonical).hexdigest(),
        "predictions_sha256": hashlib.sha256(predictions_path.read_bytes()).hexdigest(),
        "data": {
            "rows": len(rows),
            "scenarios": len(scenarios),
            "points": len(points),
            "features": list(FEATURES),
            "train_scenarios": train_scenarios,
            "test_scenarios": sorted(test_scenarios),
            "zscore": "fit on train only",
            "shape_label": "paper_like_shape; synthetic values unless source is externally supplied",
        },
        "selected_config": best_config,
        "cv": cv_results,
        "metrics": metrics,
        "metrics_by_point": per_point,
        "evidence_boundary": "synthetic fixture or user-supplied CSV; not the paper's PCSWMM result",
        "zscore_params": {"mean": zscore_params[0].tolist(), "scale": zscore_params[1].tolist()},
    }
    (output_dir / "run.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=("synthetic", "csv"), default="synthetic")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "results")
    args = parser.parse_args()
    if args.source == "csv":
        if not args.input:
            parser.error("--source csv 必须同时提供 --input")
        rows = _read_csv(args.input)
    else:
        rows = _synthetic_rows()
    result = run(rows, args.source, args.output_dir)
    print(json.dumps(result["metrics"], ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
