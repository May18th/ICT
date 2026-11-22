"""刘成帅 2023 GRGM-LSTM 的纯 NumPy 产流 smoke 和 LSTM 依赖门。

脚本只实现可审计的最小 GRGM 输入/输出契约。它不实现 LSTM，也不把合成
数据的输出当作论文结果。``--stage full`` 在 GRGM 后硬检查 torch。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np


GRID_RESOLUTION_M = 10
STATION_COUNT = 37
EVENT_COUNT = 18
TIME_STEP_H = 1.0
CELL_AREA_M2 = float(GRID_RESOLUTION_M**2)
MODE_NAMES = {0: "infiltration_excess", 1: "saturation_excess", 2: "mixed"}


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _synthetic_fixture(seed: int = 20261122) -> dict[str, Any]:
    """Create a deterministic, explicitly synthetic rainfall/grid fixture."""

    rng = np.random.default_rng(seed)
    hours = 24
    rows, cols = 4, 5
    station_xy = rng.uniform([0.0, 0.0], [40.0, 30.0], size=(STATION_COUNT, 2))
    storm_shape = np.exp(-0.5 * ((np.arange(hours) - 9.0) / 4.0) ** 2)
    event_scale = rng.uniform(0.7, 1.5, size=(EVENT_COUNT, 1, 1))
    station_scale = rng.uniform(0.8, 1.2, size=(1, 1, STATION_COUNT))
    rainfall = (
        2.0
        + 32.0 * storm_shape[None, :, None] * event_scale * station_scale
        + rng.normal(0.0, 0.35, size=(EVENT_COUNT, hours, STATION_COUNT))
    )
    rainfall = np.maximum(rainfall, 0.0)
    mode_grid = np.array(
        [[0, 0, 1, 1, 2], [0, 1, 1, 2, 2], [0, 0, 2, 2, 1], [1, 2, 2, 1, 0]],
        dtype=int,
    )
    # Labels are included only to exercise the shape contract. They are not used
    # to claim LSTM accuracy or to reproduce the paper's observed discharge.
    observed = (rainfall.mean(axis=2) * 0.0015).astype(float)
    return {
        "source_type": "synthetic",
        "grid_resolution_m": GRID_RESOLUTION_M,
        "time_step_h": TIME_STEP_H,
        "event_ids": [f"SYN-E{i:02d}" for i in range(1, EVENT_COUNT + 1)],
        "station_xy": station_xy.tolist(),
        "rainfall_mm": rainfall.tolist(),
        "mode_grid": mode_grid.tolist(),
        "parameters": {
            "f0_mm_h": 80.0,
            "fc_mm_h": 12.0,
            "decay_h_inv": 0.35,
            "storage_capacity_mm": 35.0,
        },
        "observed_discharge_m3s": observed.tolist(),
        "fixture_note": "synthetic toy data; not paper observations",
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取 JSON 输入: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("JSON 输入必须是对象")
    return data


def _as_float_array(data: dict[str, Any], key: str, ndim: int) -> np.ndarray:
    if key not in data:
        raise ValueError(f"输入缺少字段: {key}")
    try:
        value = np.asarray(data[key], dtype=float)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"字段 {key} 必须是数值数组") from exc
    if value.ndim != ndim:
        raise ValueError(f"字段 {key} 需要 {ndim} 维，实际为 {value.ndim} 维")
    if not np.all(np.isfinite(value)):
        raise ValueError(f"字段 {key} 含非有限值")
    return value


def _validate_input(data: dict[str, Any]) -> tuple[np.ndarray, np.ndarray, np.ndarray, dict[str, float]]:
    source_type = data.get("source_type")
    if not isinstance(source_type, str) or not source_type.strip():
        raise ValueError("输入必须显式提供 source_type")
    if int(data.get("grid_resolution_m", -1)) != GRID_RESOLUTION_M:
        raise ValueError("grid_resolution_m 必须为 10")
    rainfall = _as_float_array(data, "rainfall_mm", 3)
    if rainfall.shape[2] != STATION_COUNT:
        raise ValueError(f"rainfall_mm 最后一维必须是 37 个站点，实际为 {rainfall.shape[2]}")
    if np.any(rainfall < 0):
        raise ValueError("rainfall_mm 不能为负")
    station_xy = _as_float_array(data, "station_xy", 2)
    if station_xy.shape != (STATION_COUNT, 2):
        raise ValueError(f"station_xy 需要形状 (37, 2)，实际为 {station_xy.shape}")
    if "mode_grid" not in data:
        raise ValueError("输入缺少字段: mode_grid")
    try:
        mode_grid = np.asarray(data["mode_grid"], dtype=int)
    except (TypeError, ValueError) as exc:
        raise ValueError("字段 mode_grid 必须是整数数组") from exc
    if mode_grid.ndim != 2 or mode_grid.size == 0:
        raise ValueError("mode_grid 必须是非空二维数组")
    if not set(np.unique(mode_grid)).issubset(MODE_NAMES):
        raise ValueError("mode_grid 只能使用 0=超渗、1=蓄满、2=混合")
    parameters_raw = data.get("parameters")
    if not isinstance(parameters_raw, dict):
        raise ValueError("输入缺少 parameters 对象")
    required_parameters = ("f0_mm_h", "fc_mm_h", "decay_h_inv", "storage_capacity_mm")
    parameters: dict[str, float] = {}
    for name in required_parameters:
        try:
            parameters[name] = float(parameters_raw[name])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"parameters 缺少数值字段: {name}") from exc
        if not np.isfinite(parameters[name]) or parameters[name] < 0:
            raise ValueError(f"parameters.{name} 必须是非负有限值")
    if parameters["f0_mm_h"] < parameters["fc_mm_h"]:
        raise ValueError("parameters.f0_mm_h 不能小于 fc_mm_h")
    event_ids = data.get("event_ids")
    if event_ids is not None and (not isinstance(event_ids, list) or len(event_ids) != rainfall.shape[0]):
        raise ValueError("event_ids 长度必须与 rainfall_mm 的事件数一致")
    observed = data.get("observed_discharge_m3s")
    if observed is not None:
        observed_array = _as_float_array(data, "observed_discharge_m3s", 2)
        if observed_array.shape != rainfall.shape[:2]:
            raise ValueError("observed_discharge_m3s 需要形状 [events, hours]")
    return rainfall, station_xy, mode_grid, parameters


def _grid_centres(mode_grid: np.ndarray) -> np.ndarray:
    rows, cols = mode_grid.shape
    return np.array(
        [[(col + 0.5) * GRID_RESOLUTION_M, (row + 0.5) * GRID_RESOLUTION_M] for row in range(rows) for col in range(cols)],
        dtype=float,
    )


def _interpolate_to_grid(rainfall: np.ndarray, station_xy: np.ndarray, mode_grid: np.ndarray) -> np.ndarray:
    centres = _grid_centres(mode_grid)
    distances = np.linalg.norm(centres[:, None, :] - station_xy[None, :, :], axis=2)
    weights = 1.0 / np.maximum(distances, 1e-6) ** 2
    weights /= weights.sum(axis=1, keepdims=True)
    grid = np.einsum("ets,cs->etc", rainfall, weights, optimize=True)
    return grid.reshape(rainfall.shape[0], rainfall.shape[1], *mode_grid.shape)


def run_grgm(data: dict[str, Any]) -> dict[str, Any]:
    rainfall, station_xy, mode_grid, parameters = _validate_input(data)
    grid_rain = _interpolate_to_grid(rainfall, station_xy, mode_grid)
    events, hours, rows, cols = grid_rain.shape
    cells = rows * cols
    rain_cells = grid_rain.reshape(events, hours, cells)
    modes = mode_grid.reshape(cells)
    storage = np.zeros((events, cells), dtype=float)
    runoff = np.zeros_like(rain_cells)
    infiltration = np.zeros_like(rain_cells)
    storage_history = np.zeros_like(rain_cells)
    for time_idx in range(hours):
        rain_t = rain_cells[:, time_idx, :]
        horton_capacity = parameters["fc_mm_h"] + (
            parameters["f0_mm_h"] - parameters["fc_mm_h"]
        ) * np.exp(-parameters["decay_h_inv"] * time_idx * TIME_STEP_H)
        horton_runoff = np.maximum(rain_t - horton_capacity * TIME_STEP_H, 0.0)
        horton_infiltration = rain_t - horton_runoff
        available_storage = np.maximum(parameters["storage_capacity_mm"] - storage, 0.0)
        saturation_infiltration = np.minimum(rain_t, available_storage)
        saturation_runoff = rain_t - saturation_infiltration
        cell_runoff = np.where(modes == 0, horton_runoff, np.where(modes == 1, saturation_runoff, 0.5 * (horton_runoff + saturation_runoff)))
        cell_infiltration = rain_t - cell_runoff
        storage += cell_infiltration
        runoff[:, time_idx, :] = cell_runoff
        infiltration[:, time_idx, :] = cell_infiltration
        storage_history[:, time_idx, :] = storage
    residual = rain_cells - runoff - infiltration
    if not np.allclose(residual, 0.0, atol=1e-10):
        raise RuntimeError(f"GRGM 水量守恒失败，最大残差 {float(np.max(np.abs(residual)))} mm")
    runoff_mm = runoff.reshape(events, hours, rows, cols)
    infiltration_mm = infiltration.reshape(events, hours, rows, cols)
    storage_mm = storage_history.reshape(events, hours, rows, cols)
    runoff_volume_m3 = runoff.sum(axis=2) * CELL_AREA_M2 / 1000.0
    discharge_m3s = runoff_volume_m3 / (TIME_STEP_H * 3600.0)
    rain_volume_m3 = rain_cells.sum(axis=2) * CELL_AREA_M2 / 1000.0
    infiltration_volume_m3 = infiltration.sum(axis=2) * CELL_AREA_M2 / 1000.0
    return {
        "grid_rain_mm": grid_rain,
        "runoff_mm": runoff_mm,
        "infiltration_mm": infiltration_mm,
        "storage_mm": storage_mm,
        "runoff_volume_m3": runoff_volume_m3,
        "discharge_m3s": discharge_m3s,
        "mass_residual_mm": residual.reshape(events, hours, rows, cols),
        "rain_volume_m3": rain_volume_m3,
        "infiltration_volume_m3": infiltration_volume_m3,
    }


def _require_torch():
    try:
        import torch
    except ImportError as exc:
        raise RuntimeError("缺少 PyTorch。LSTM 依赖门失败，不使用 RNN 或线性模型替代。") from exc
    return torch


def _torch_version() -> str:
    return str(_require_torch().__version__)


def _nse(observed: np.ndarray, simulated: np.ndarray) -> float:
    """Nash-Sutcliffe efficiency; returns NaN when the observed series has no variance."""

    denominator = float(np.sum((observed - observed.mean()) ** 2))
    if denominator <= 0.0:
        return float("nan")
    return float(1.0 - np.sum((observed - simulated) ** 2) / denominator)


def run_lstm(
    arrays: dict[str, np.ndarray],
    seed: int = 42,
    epochs: int = 400,
    hidden_size: int = 16,
    learning_rate: float = 0.01,
    holdout_events: int = 4,
    observed: np.ndarray | None = None,
    target_source: str = "grgm",
) -> dict[str, Any]:
    """Train a small LSTM from grid runoff to discharge, with a held-out event split.

    这是**闭环自洽检验**：标签（discharge_m3s）由同一份 GRGM 计算得到，所以 NSE 高只说明
    "接口连通、能拟合自己生成的标签"，不能证明对真实洪水的预报能力。留出事件只保证不是
    逐点记忆，不改变这一点。
    """

    torch = _require_torch()
    torch.manual_seed(seed)
    torch.set_num_threads(1)

    runoff = np.asarray(arrays["runoff_mm"], dtype=float)
    # 有实测汇流就用实测当标签（监督），否则退回 GRGM 自算的流量（闭环自洽）
    discharge = np.asarray(observed if observed is not None else arrays["discharge_m3s"], dtype=float)
    task = "observed_discharge" if observed is not None else "closed_loop_self_consistency"
    events, hours = discharge.shape
    features = runoff.reshape(events, hours, -1)

    holdout = max(1, min(holdout_events, events - 1))
    train_idx = np.arange(events - holdout)
    test_idx = np.arange(events - holdout, events)

    x_mean = features[train_idx].mean(axis=(0, 1))
    x_std = features[train_idx].std(axis=(0, 1))
    x_std = np.where(x_std < 1e-8, 1.0, x_std)
    y_mean = float(discharge[train_idx].mean())
    y_std = float(discharge[train_idx].std()) or 1.0

    x_norm = (features - x_mean) / x_std
    y_norm = (discharge - y_mean) / y_std
    x_train = torch.tensor(x_norm[train_idx], dtype=torch.float32)
    y_train = torch.tensor(y_norm[train_idx].reshape(train_idx.size, hours, 1), dtype=torch.float32)
    x_test = torch.tensor(x_norm[test_idx], dtype=torch.float32)

    class _LSTMRegressor(torch.nn.Module):
        def __init__(self, input_size: int, hidden: int) -> None:
            super().__init__()
            self.lstm = torch.nn.LSTM(input_size, hidden, batch_first=True)
            self.head = torch.nn.Linear(hidden, 1)

        def forward(self, x):  # noqa: ANN001 - torch 张量
            output, _ = self.lstm(x)
            return self.head(output)

    model = _LSTMRegressor(features.shape[2], hidden_size)
    optimiser = torch.optim.Adam(model.parameters(), lr=learning_rate)
    loss_fn = torch.nn.MSELoss()
    loss_trace = []
    for epoch in range(epochs):
        model.train()
        optimiser.zero_grad()
        loss = loss_fn(model(x_train), y_train)
        loss.backward()
        optimiser.step()
        if epoch % 100 == 0 or epoch == epochs - 1:
            loss_trace.append({"epoch": epoch, "mse_normalised": float(loss.item())})

    model.eval()
    with torch.no_grad():
        pred_train = model(x_train).numpy().reshape(train_idx.size, hours) * y_std + y_mean
        pred_test = model(x_test).numpy().reshape(test_idx.size, hours) * y_std + y_mean

    observed_train = discharge[train_idx]
    observed_test = discharge[test_idx]
    naive_test = np.full_like(observed_test, y_mean)
    prediction = np.full_like(discharge, np.nan)
    prediction[train_idx] = pred_train
    prediction[test_idx] = pred_test

    return {
        "metrics": {
            "task": task,
            "target_source": target_source,
            "nse_train": _nse(observed_train.ravel(), pred_train.ravel()),
            "nse_test": _nse(observed_test.ravel(), pred_test.ravel()),
            "rmse_test_m3s": float(np.sqrt(np.mean((observed_test - pred_test) ** 2))),
            "nse_test_mean_baseline": _nse(observed_test.ravel(), naive_test.ravel()),
            "nse_test_per_event": [
                _nse(observed_test[i], pred_test[i]) for i in range(observed_test.shape[0])
            ],
        },
        "hyperparameters": {
            "seed": seed,
            "epochs": epochs,
            "hidden_size": hidden_size,
            "learning_rate": learning_rate,
            "optimizer": "Adam",
            "loss": "MSE on min-max-free z-scored discharge",
            "input_features": int(features.shape[2]),
            "torch_threads": 1,
        },
        "split": {
            "train_events": [int(i) for i in train_idx],
            "test_events": [int(i) for i in test_idx],
        },
        "loss_trace": loss_trace,
        "prediction_m3s": prediction,
    }


def _write_result(output_dir: Path, result: dict[str, Any], arrays: dict[str, np.ndarray] | None) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    if arrays is not None:
        extra = {k: v for k, v in arrays.items() if k == "lstm_prediction_m3s"}
        np.savez_compressed(
            output_dir / "runoff_outputs.npz",
            grid_rain_mm=arrays["grid_rain_mm"],
            runoff_mm=arrays["runoff_mm"],
            infiltration_mm=arrays["infiltration_mm"],
            storage_mm=arrays["storage_mm"],
            runoff_volume_m3=arrays["runoff_volume_m3"],
            discharge_m3s=arrays["discharge_m3s"],
            mass_residual_mm=arrays["mass_residual_mm"],
            rain_volume_m3=arrays["rain_volume_m3"],
            infiltration_volume_m3=arrays["infiltration_volume_m3"],
            **extra,
        )
        result["outputs"] = {"arrays": "runoff_outputs.npz"}
    (output_dir / "run.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _result_metadata(data: dict[str, Any], arrays: dict[str, np.ndarray], input_hash: str, stage: str) -> dict[str, Any]:
    rainfall = np.asarray(data["rainfall_mm"], dtype=float)
    grid_shape = list(np.asarray(data["mode_grid"], dtype=int).shape)
    residual = arrays["mass_residual_mm"]
    rain_volume = float(arrays["rain_volume_m3"].sum())
    infiltration_volume = float(arrays["infiltration_volume_m3"].sum())
    runoff_volume = float(arrays["runoff_volume_m3"].sum())
    return {
        "source_type": str(data["source_type"]),
        "paper_result_claim": False,
        "stage": stage,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "command": " ".join(sys.argv),
        "input_sha256": input_hash,
        "environment": {"python": sys.version.split()[0], "platform": platform.platform(), "numpy": np.__version__},
        "method": {"name": "GRGM runoff smoke", "grid_resolution_m": GRID_RESOLUTION_M, "grid_shape": grid_shape, "station_count": STATION_COUNT, "event_count": int(rainfall.shape[0]), "time_steps": int(rainfall.shape[1]), "mode_names": MODE_NAMES},
        "shapes": {name: list(value.shape) for name, value in arrays.items() if isinstance(value, np.ndarray)},
        "conservation": {"rain_volume_m3": rain_volume, "infiltration_volume_m3": infiltration_volume, "runoff_volume_m3": runoff_volume, "volume_residual_m3": rain_volume - infiltration_volume - runoff_volume, "max_abs_step_residual_mm": float(np.max(np.abs(residual)))},
        "lstm_dependency": "not_checked",
        "evidence_boundary": "synthetic fixture or user-supplied JSON; no claim about the paper's observed NSE or flood forecast accuracy",
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="GRGM NumPy smoke and LSTM dependency gate")
    parser.add_argument("--source", choices=("synthetic", "json"), default="synthetic")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--stage", choices=("grgm", "full", "lstm-gate"), default="grgm")
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "results")
    args = parser.parse_args()
    if args.stage == "lstm-gate":
        try:
            version = _torch_version()
        except RuntimeError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        print(json.dumps({"torch": version}, ensure_ascii=False))
        return 0
    if args.source == "json":
        if args.input is None:
            parser.error("--source json 必须同时提供 --input")
        data = _read_json(args.input)
    else:
        if args.input is not None:
            parser.error("--source synthetic 不接受 --input")
        data = _synthetic_fixture()
    input_hash = hashlib.sha256(_canonical_json(data)).hexdigest()
    arrays = run_grgm(data)
    result = _result_metadata(data, arrays, input_hash, args.stage)
    if args.stage == "full":
        try:
            torch_version = _torch_version()
            observed = data.get("observed_discharge_m3s")
            lstm = run_lstm(
                arrays,
                observed=None if observed is None else np.asarray(observed, dtype=float),
                target_source="grgm" if observed is None else str(data["source_type"]),
            )
        except RuntimeError as exc:
            result["lstm_dependency"] = {"status": "blocked", "error": str(exc)}
            _write_result(args.output_dir, result, arrays)
            print(str(exc), file=sys.stderr)
            return 2
        result["lstm_dependency"] = {"status": "available", "torch": torch_version}
        result["environment"]["torch"] = torch_version
        result["lstm"] = {k: v for k, v in lstm.items() if k != "prediction_m3s"}
        target_source = result["lstm"]["metrics"]["target_source"]
        if target_source == "grgm":
            result["lstm"]["evidence_boundary"] = (
                "没有观测标签，目标量是同一份 GRGM 体积换算出的流量，属闭环自洽；"
                "NSE 接近 1 只证明接口与训练流程可用，不代表洪水预报能力。"
            )
        else:
            result["lstm"]["evidence_boundary"] = (
                f"目标量来自输入自带的 observed_discharge_m3s，其来源标记为 {target_source}；"
                "合成观测（当前 fixture 用面雨量线性换算）仍不是实测，NSE 不可当作预报能力，"
                "要检验能力必须提供论文 18 场实测洪水。"
            )
        arrays["lstm_prediction_m3s"] = lstm["prediction_m3s"]
    _write_result(args.output_dir, result, arrays)
    print(json.dumps({"source_type": result["source_type"], "max_abs_step_residual_mm": result["conservation"]["max_abs_step_residual_mm"], "stage": args.stage}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
