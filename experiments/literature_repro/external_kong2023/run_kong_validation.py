#!/usr/bin/env python
"""真观测检验：用 Kong et al. (2023) 的深圳 10 条易涝路实测数据跑「雨量 → 水位」。

和另外四条路线的区别：这里的标签是**实测水位**（深圳市政务数据开放平台 171 个积水测站中的
10 个易涝点），不是合成序列、不是自算流量。所以它第一次能给出一条有意义的 NSE。

但样本量的限制是硬的：只有 2019-06-11 一场，10 条序列，每条 10—56 个观测。
因此评估方式必须是**按路段留出**（leave-one-road-out），而不是按时间点切分——
同一条路上的相邻观测高度自相关，按时间切会把答案漏进训练集。

两种产出：

1. ``observed_summary``：每条路的雨量总量、峰值水位、峰现滞后。纯观测统计，无模型。
2. ``ridge_loro``：岭回归在「过去 N 分钟雨量」上拟合水位，按路段留出。给 pooled NSE、
   逐路 NSE 与均值基线，用来判断「雨量里到底有没有水位信号」。

边界写在 ``evidence_boundary`` 里：n=10 条序列、单场、深圳道路而非成都片区，
结论只能支持「方法在真观测上可训练且方向正确」，不能支撑任何精度承诺。
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))

import load_kong2023 as kong  # noqa: E402

#: 用于构造特征的回看窗口（分钟）。
LOOKBACK_WINDOWS = (5, 15, 30, 60, 120)


def _features(minutes: np.ndarray, rain: np.ndarray) -> tuple[np.ndarray, list[str]]:
    """只用雨量与时间构造特征，绝不使用水位的历史值。

    水位序列自相关极强，一旦把前一时刻水位放进特征，NSE 会虚高到接近 1，
    测的就只是「上一刻和这一刻差多少」，与降雨驱动无关。
    """

    columns = [rain.astype(float)]
    names = ["rain_now_mm"]
    for window in LOOKBACK_WINDOWS:
        steps = max(1, int(round(window / kong.RAIN_INTERVAL_MIN)))
        cumulative = np.convolve(rain, np.ones(steps), mode="full")[: rain.size]
        columns.append(cumulative)
        names.append(f"rain_last_{window}min_mm")

    cumulative_total = np.cumsum(rain)
    columns.append(cumulative_total)
    names.append("rain_cumulative_mm")

    elapsed = np.clip(minutes, 0.0, None)
    columns.append(elapsed / 60.0)
    names.append("elapsed_hours")

    return np.column_stack(columns), names


def _nse(observed: np.ndarray, simulated: np.ndarray) -> float:
    denominator = float(np.sum((observed - observed.mean()) ** 2))
    if denominator <= 0.0:
        return float("nan")
    return float(1.0 - np.sum((observed - simulated) ** 2) / denominator)


def run_ridge_loro(bundles: list[kong.RoadBundle], alpha: float) -> dict[str, object]:
    from sklearn.linear_model import Ridge

    grids = {b.road_id: kong.to_common_grid(b) for b in bundles}
    feature_blocks = {}
    names: list[str] = []
    for road_id, (minutes, rain, level) in grids.items():
        block, names = _features(minutes, rain)
        feature_blocks[road_id] = (block, level)

    roads = list(feature_blocks)
    pooled_observed: list[np.ndarray] = []
    pooled_predicted: list[np.ndarray] = []
    pooled_baseline: list[np.ndarray] = []
    per_road: dict[str, dict[str, float]] = {}

    for held_out in roads:
        train_roads = [r for r in roads if r != held_out]
        x_train = np.vstack([feature_blocks[r][0] for r in train_roads])
        y_train = np.concatenate([feature_blocks[r][1] for r in train_roads])
        x_test, y_test = feature_blocks[held_out]

        # 标准化只用训练段统计量，避免把留出路的分布泄漏进预处理
        mean = x_train.mean(axis=0)
        scale = x_train.std(axis=0)
        scale = np.where(scale < 1e-9, 1.0, scale)
        model = Ridge(alpha=alpha).fit((x_train - mean) / scale, y_train)
        prediction = model.predict((x_test - mean) / scale)

        per_road[held_out] = {
            "n": int(y_test.size),
            "nse": round(_nse(y_test, prediction), 4),
            "rmse_m": round(float(np.sqrt(np.mean((y_test - prediction) ** 2))), 4),
            "observed_peak_m": round(float(y_test.max()), 3),
            "predicted_peak_m": round(float(prediction.max()), 3),
        }
        pooled_observed.append(y_test)
        pooled_predicted.append(prediction)
        pooled_baseline.append(np.full_like(y_test, y_train.mean()))

    observed_all = np.concatenate(pooled_observed)
    predicted_all = np.concatenate(pooled_predicted)
    baseline_all = np.concatenate(pooled_baseline)
    road_nse = [v["nse"] for v in per_road.values() if np.isfinite(v["nse"])]

    return {
        "model": "ridge",
        "alpha": alpha,
        "split": "leave-one-road-out（10 折，每条路整段留出）",
        "features": names,
        "pooled_nse": round(_nse(observed_all, predicted_all), 4),
        "pooled_rmse_m": round(float(np.sqrt(np.mean((observed_all - predicted_all) ** 2))), 4),
        "pooled_nse_mean_baseline": round(_nse(observed_all, baseline_all), 4),
        "per_road_median_nse": round(float(np.median(road_nse)), 4) if road_nse else None,
        "per_road_positive_nse_count": int(sum(1 for v in road_nse if v > 0)),
        "per_road": per_road,
    }


def run_time_holdout(bundles: list[kong.RoadBundle], alpha: float, train_fraction: float) -> dict[str, object]:
    """逐路按时间切分：前段训练、后段测试。

    这不是泛化检验——同一条路的相邻观测强自相关，后段水位与训练段高度相关，
    NSE 会明显偏高。放这里是为了和 leave-one-road-out 对照：
    如果时间切分能拟合而跨路不能，说明缺的不是模型而是**点位属性**。
    """

    from sklearn.linear_model import Ridge

    per_road: dict[str, dict[str, float]] = {}
    pooled_observed: list[np.ndarray] = []
    pooled_predicted: list[np.ndarray] = []
    rising_obs: list[np.ndarray] = []
    rising_pred: list[np.ndarray] = []
    falling_obs: list[np.ndarray] = []
    falling_pred: list[np.ndarray] = []

    for bundle in bundles:
        minutes, rain, level = kong.to_common_grid(bundle)
        block, _ = _features(minutes, rain)
        cut = max(2, int(round(level.size * train_fraction)))
        if cut >= level.size:
            continue
        x_train, y_train = block[:cut], level[:cut]
        x_test, y_test = block[cut:], level[cut:]

        mean = x_train.mean(axis=0)
        scale = x_train.std(axis=0)
        scale = np.where(scale < 1e-9, 1.0, scale)
        model = Ridge(alpha=alpha).fit((x_train - mean) / scale, y_train)
        prediction = model.predict((x_test - mean) / scale)

        per_road[bundle.road_id] = {
            "n_test": int(y_test.size),
            "nse": round(_nse(y_test, prediction), 4),
        }
        pooled_observed.append(y_test)
        pooled_predicted.append(prediction)

        # 涨水/退水分开统计：退水段的驱动力是蓄量释放而不是当时的雨强，
        # 分开看能把「模型缺状态变量」和「模型本身烂」区分开。
        delta = np.diff(level, prepend=level[0])[cut:]
        rising = delta > 0
        if rising.any():
            rising_obs.append(y_test[rising])
            rising_pred.append(prediction[rising])
        if (~rising).any():
            falling_obs.append(y_test[~rising])
            falling_pred.append(prediction[~rising])

    observed_all = np.concatenate(pooled_observed)
    predicted_all = np.concatenate(pooled_predicted)
    road_nse = [v["nse"] for v in per_road.values() if np.isfinite(v["nse"])]
    return {
        "model": "ridge",
        "alpha": alpha,
        "split": f"逐路时间切分（前 {train_fraction:.0%} 训练，后段测试）",
        "pooled_nse": round(_nse(observed_all, predicted_all), 4),
        "per_road_median_nse": round(float(np.median(road_nse)), 4) if road_nse else None,
        "per_road_positive_nse_count": int(sum(1 for v in road_nse if v > 0)),
        "phase_diagnostic": {
            "note": (
                "按测试段内水位是否上升分组。退水段只剩雨停后的衰减，"
                "纯雨量特征无从表达，所以两组差距能说明「缺少水位/蓄量状态」而不是「特征没用」。"
            ),
            "rising": _phase_metric(rising_obs, rising_pred),
            "falling": _phase_metric(falling_obs, falling_pred),
        },
        "per_road": per_road,
    }


def _phase_metric(observed: list[np.ndarray], predicted: list[np.ndarray]) -> dict[str, object]:
    if not observed:
        return {"n": 0, "rmse_m": None, "nse": None}
    obs = np.concatenate(observed)
    pred = np.concatenate(predicted)
    return {
        "n": int(obs.size),
        "rmse_m": round(float(np.sqrt(np.mean((obs - pred) ** 2))), 4),
        "nse": round(_nse(obs, pred), 4),
        "observed_mean_m": round(float(obs.mean()), 3),
        "predicted_mean_m": round(float(pred.mean()), 3),
    }


def run_random_holdout(
    bundles: list[kong.RoadBundle], alpha: float, holdout_fraction: float, seed: int
) -> dict[str, object]:
    """逐路随机留出时间点（默认 20%）。

    这是**插值**而不是预测：相邻 5 min 的水位几乎相同，随机留出的点被自己的邻居包围，
    NSE 必然偏高。它的唯一用途是给出谱系的另一头——
    如果连随机留出都低，说明雨量特征根本没解释力；如果随机留出很高而跨路很低，
    说明关系是「每条路各自一套」而不是「全城一套」，缺的是点位属性。
    """

    from sklearn.linear_model import Ridge

    rng = np.random.default_rng(seed)
    pooled_observed: list[np.ndarray] = []
    pooled_predicted: list[np.ndarray] = []
    rising_obs: list[np.ndarray] = []
    rising_pred: list[np.ndarray] = []
    falling_obs: list[np.ndarray] = []
    falling_pred: list[np.ndarray] = []
    per_road: dict[str, dict[str, float]] = {}

    for bundle in bundles:
        minutes, rain, level = kong.to_common_grid(bundle)
        block, _ = _features(minutes, rain)
        n = level.size
        holdout = max(1, int(round(n * holdout_fraction)))
        test_index = np.sort(rng.choice(n, size=holdout, replace=False))
        train_mask = np.ones(n, dtype=bool)
        train_mask[test_index] = False

        x_train, y_train = block[train_mask], level[train_mask]
        x_test, y_test = block[test_index], level[test_index]

        mean = x_train.mean(axis=0)
        scale = x_train.std(axis=0)
        scale = np.where(scale < 1e-9, 1.0, scale)
        model = Ridge(alpha=alpha).fit((x_train - mean) / scale, y_train)
        prediction = model.predict((x_test - mean) / scale)

        per_road[bundle.road_id] = {"n_test": int(y_test.size), "nse": round(_nse(y_test, prediction), 4)}
        pooled_observed.append(y_test)
        pooled_predicted.append(prediction)

        delta = np.diff(level, prepend=level[0])[test_index]
        rising = delta > 0
        if rising.any():
            rising_obs.append(y_test[rising])
            rising_pred.append(prediction[rising])
        if (~rising).any():
            falling_obs.append(y_test[~rising])
            falling_pred.append(prediction[~rising])

    observed_all = np.concatenate(pooled_observed)
    predicted_all = np.concatenate(pooled_predicted)
    road_nse = [v["nse"] for v in per_road.values() if np.isfinite(v["nse"])]
    return {
        "model": "ridge",
        "alpha": alpha,
        "split": f"逐路随机留出 {holdout_fraction:.0%} 时间点（seed {seed}），属插值上界",
        "pooled_nse": round(_nse(observed_all, predicted_all), 4),
        "pooled_rmse_m": round(float(np.sqrt(np.mean((observed_all - predicted_all) ** 2))), 4),
        "per_road_median_nse": round(float(np.median(road_nse)), 4) if road_nse else None,
        "per_road_positive_nse_count": int(sum(1 for v in road_nse if v > 0)),
        "phase_diagnostic": {
            "rising": _phase_metric(rising_obs, rising_pred),
            "falling": _phase_metric(falling_obs, falling_pred),
        },
        "per_road": per_road,
    }


def run_self_fit(bundles: list[kong.RoadBundle], alpha: float) -> dict[str, object]:
    """每条路各拟合一份模型并在自身数据上回测。

    这是**上界**，不是精度：同一条序列同时进训练和评估，任何平滑回归都能拿到高 NSE。
    它的用处只有一个——如果连上界都低，说明这组雨量特征对水位根本没有解释力，
    跨路失败就不能归咎于泛化。
    """

    from sklearn.linear_model import Ridge

    per_road: dict[str, float] = {}
    for bundle in bundles:
        minutes, rain, level = kong.to_common_grid(bundle)
        block, _ = _features(minutes, rain)
        model = Ridge(alpha=alpha).fit(block, level)
        per_road[bundle.road_id] = round(_nse(level, model.predict(block)), 4)

    finite = [v for v in per_road.values() if np.isfinite(v)]
    return {
        "model": "ridge",
        "alpha": alpha,
        "kind": "in-sample upper bound（不可作为精度指标）",
        "per_road_median_nse": round(float(np.median(finite)), 4) if finite else None,
        "per_road_min_nse": round(float(np.min(finite)), 4) if finite else None,
        "per_road": per_road,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-root",
        type=Path,
        default=Path("_external/kong2023"),
        help="fetch_kong2023.py 的落地目录（含 Assets/）",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "results",
        help="结果目录",
    )
    parser.add_argument("--alpha", type=float, default=1.0, help="Ridge 正则强度")
    parser.add_argument(
        "--train-fraction",
        type=float,
        default=0.7,
        help="时间切分诊断中的训练段占比",
    )
    parser.add_argument(
        "--source",
        choices=("observed",),
        default="observed",
        help="固定为 observed；本脚本不接受合成数据",
    )
    args = parser.parse_args()

    if "samples" in args.output_dir.parts:
        print("拒绝写入 samples/：该目录只读。", file=sys.stderr)
        return 2

    bundles = kong.load_bundle(args.data_root)
    summaries = [kong.summarise(b) for b in bundles]
    regression = run_ridge_loro(bundles, args.alpha)
    time_split = run_time_holdout(bundles, args.alpha, args.train_fraction)
    random_split = run_random_holdout(bundles, args.alpha, 0.2, 42)
    self_fit = run_self_fit(bundles, args.alpha)

    peaks = [s["peak_level_m"] for s in summaries]
    lags = [s["lag_minutes"] for s in summaries]
    result = {
        "source_type": "observed",
        "dataset": {
            "name": "Kong et al. 2023 validation bundle — 深圳 10 条易涝路",
            "zenodo_doi": "10.5281/zenodo.7894921",
            "license": "CC BY 4.0",
            "event": "2019-06-11",
            "rain_interval_min": kong.RAIN_INTERVAL_MIN,
            "level_unit": "m",
            "rain_unit": "mm per 5 min",
            "note": (
                "论文的率定事件是 2015-05-09 / 05-23，Zenodo 只公开了这两场的出租车通过率计数，"
                "没有对应水位；能用的真观测水位只有 2019-06-11 这一场。"
            ),
        },
        "paper_result_claim": False,
        "command": " ".join(
            [
                "python",
                "experiments/literature_repro/external_kong2023/run_kong_validation.py",
                "--source",
                args.source,
                "--data-root",
                _display_path(args.data_root),
                "--alpha",
                str(args.alpha),
            ]
        ),
        "environment": _environment(),
        "input_sha256": _input_digest(args.data_root),
        "observed_summary": summaries,
        "observed_aggregate": {
            "roads": len(summaries),
            "peak_level_m_range": [round(min(peaks), 3), round(max(peaks), 3)],
            "peak_level_m_median": round(float(np.median(peaks)), 3),
            "lag_minutes_median": round(float(np.median(lags)), 1),
            "lag_minutes_range": [round(min(lags), 1), round(max(lags), 1)],
            "total_rain_mm_range": [
                round(min(s["total_rain_mm"] for s in summaries), 2),
                round(max(s["total_rain_mm"] for s in summaries), 2),
            ],
        },
        "ridge_loro": regression,
        "ridge_time_holdout": time_split,
        "ridge_random_holdout": random_split,
        "ridge_self_fit_upper_bound": self_fit,
        "evidence_boundary": (
            "标签是深圳市政务数据开放平台 171 个积水测站中的 10 个易涝点在 2019-06-11 的实测水位，"
            "不是合成序列。但只有单场、10 条序列、逐路 10—56 个观测，按路段留出后独立单元仅 10 个，"
            "NSE 受单条路支配，不构成精度结论；深圳道路积水与成都片区管网不是同一对象，"
            "结论只能支持「雨量→水位存在可学习信号、接口在真实测上可训练」，"
            "不能写成本项目的预测精度，也不能替代论文作者那套 PCSWMM 49 情景结果。"
        ),
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(result, ensure_ascii=False, indent=2)
    (args.output_dir / "run.json").write_text(payload + "\n", encoding="utf-8", newline="\n")
    _write_series_csv(bundles, args.output_dir / "observed_series.csv")

    print(
        "实测峰值水位 {:.2f}–{:.2f} m（中位 {:.2f}），峰现滞后中位 {:.0f} min".format(
            result["observed_aggregate"]["peak_level_m_range"][0],
            result["observed_aggregate"]["peak_level_m_range"][1],
            result["observed_aggregate"]["peak_level_m_median"],
            result["observed_aggregate"]["lag_minutes_median"],
        )
    )
    print(
        "岭回归（按路段留出）pooled NSE {:.3f} / 均值基线 {:.3f} / pooled RMSE {:.3f} m".format(
            regression["pooled_nse"], regression["pooled_nse_mean_baseline"], regression["pooled_rmse_m"]
        )
    )
    print(
        "逐路 NSE 中位 {:.3f}，10 条中 {} 条为正".format(
            regression["per_road_median_nse"], regression["per_road_positive_nse_count"]
        )
    )
    print(
        "诊断｜逐路时间切分 pooled NSE {:.3f}（中位 {:.3f}，{} 条为正）".format(
            time_split["pooled_nse"],
            time_split["per_road_median_nse"],
            time_split["per_road_positive_nse_count"],
        )
    )
    print(
        "诊断｜随机点留出 pooled NSE {:.3f}（中位 {:.3f}）｜涨水段 RMSE {} / 退水段 RMSE {}".format(
            random_split["pooled_nse"],
            random_split["per_road_median_nse"],
            random_split["phase_diagnostic"]["rising"]["rmse_m"],
            random_split["phase_diagnostic"]["falling"]["rmse_m"],
        )
    )
    print(
        "诊断｜逐路自拟合 NSE 中位 {:.3f}（上界，非精度）".format(self_fit["per_road_median_nse"])
    )

    if not np.isfinite(regression["pooled_nse"]):
        print("pooled NSE 非有限值，评估失效。", file=sys.stderr)
        return 1
    return 0


def _write_series_csv(bundles: list[kong.RoadBundle], path: Path) -> None:
    lines = ["road_id,relative_minutes,rain_mm_per_5min,water_level_m"]
    for bundle in bundles:
        minutes, rain, level = kong.to_common_grid(bundle)
        for minute, r, l in zip(minutes, rain, level):
            lines.append(f"{bundle.road_id},{minute:.0f},{r:.2f},{l:.3f}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _display_path(path: Path) -> str:
    """记录命令时用仓库相对路径；仓外目录只写末级名，避免把本机绝对路径写进结果。"""

    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return f"<external>/{path.name}"


def _input_digest(data_root: Path) -> dict[str, str]:
    import hashlib

    digests = {}
    for name in ("water_level_10.xlsx", "rainfall_10.xlsx"):
        path = Path(data_root) / "Assets" / "Validation data" / name
        digests[name] = hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else "missing"
    return digests


def _environment() -> dict[str, str]:
    import platform

    import openpyxl
    import sklearn

    return {
        "python": platform.python_version(),
        "numpy": np.__version__,
        "scikit-learn": sklearn.__version__,
        "openpyxl": openpyxl.__version__,
        "platform": platform.platform(),
    }


if __name__ == "__main__":
    raise SystemExit(main())
