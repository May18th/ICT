"""王小杰 2024 城市洪涝 1D-2D 双向耦合的交换模式 smoke。

脚本实现可审计的最小双向耦合：地下管网按一维管段容积路由，地表按二维有限体积漫流，
两者通过"交换单元"（检查井 / 雨水口）按孔口流量公式双向交换，并在每个子步内同步求解。
三种交换模式（检查井法、雨水口法、雨水口-检查井法）可切换，用于比较排水系统对地表
淹没面积的削减方向。

它不实现论文的 SWMM 完整管网、不实现二维浅水方程、不使用 Glasgow 案例的几何与观测，
因此输出只能标 ``toy``，不能声称复现论文的 9.3% / 23.2% / 24.5% 等数值。
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

CELL_M = 10.0
CELL_AREA_M2 = CELL_M * CELL_M
G = 9.81
MODES = ("none", "manhole", "inlet", "inlet_manhole")
SUBSTEPS_PER_STEP = 40


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _synthetic_fixture(seed: int = 20261122) -> dict[str, Any]:
    """Toy terrain, one trunk pipe chain, rain series, and two exchange unit families."""

    rng = np.random.default_rng(seed)
    rows, cols = 18, 24
    yy, xx = np.mgrid[0:rows, 0:cols]
    base = 8.0 - 0.05 * yy - 0.02 * xx
    bowl_r = np.hypot((yy - 11.0) * 0.9, (xx - 12.0) * 0.7)
    dem = np.round(base - 0.9 * np.exp(-(bowl_r**2) / 12.0) + rng.normal(0.0, 0.01, (rows, cols)), 4)

    steps, dt_s = 12, 300.0
    rain_mm_h = np.array([0, 0, 12, 30, 48, 42, 26, 14, 6, 2, 0, 0], dtype=float)
    dt_h = dt_s / 3600.0
    rain_mm = rain_mm_h * dt_h

    # 一条主干管：8 个管段单元，沿洼地底部由西向东
    pipe_cells = [(11, 8 + i) for i in range(8)]
    pipe_area = 2.0  # m2，等效过水断面积
    pipe_len = CELL_M
    pipe_invert = [float(dem[r, c]) - 2.0 for r, c in pipe_cells]
    manholes = [{"unit_id": f"MH-{i + 1:02d}", "row": r, "col": c, "pipe_index": i,
                 "orifice_m2": 0.5, "crest_m": float(dem[r, c]) + 0.0}
                for i, (r, c) in enumerate(pipe_cells)]
    inlets = [
        {"unit_id": "IN-01", "row": 10, "col": 11, "pipe_index": 3, "orifice_m2": 0.2,
         "crest_m": float(dem[10, 11]) + 0.0},
        {"unit_id": "IN-02", "row": 12, "col": 12, "pipe_index": 4, "orifice_m2": 0.2,
         "crest_m": float(dem[12, 12]) + 0.0},
        {"unit_id": "IN-03", "row": 11, "col": 13, "pipe_index": 5, "orifice_m2": 0.2,
         "crest_m": float(dem[11, 13]) + 0.0},
    ]

    return {
        "source_type": "toy",
        "grid_resolution_m": CELL_M,
        "dem_m": dem.tolist(),
        "manning_surface": 0.035,
        "manning_pipe": 0.013,
        "dt_s": dt_s,
        "rain_mm": rain_mm.tolist(),
        "pipe": {
            "cells": [{"row": r, "col": c} for r, c in pipe_cells],
            "area_m2": pipe_area,
            "length_m": pipe_len,
            "invert_m": pipe_invert,
            "initial_level_m": 0.0,
            "outfall_index": len(pipe_cells) - 1,
            "outfall_stage_m": pipe_invert[-1] + 0.1,
        },
        "manholes": manholes,
        "inlets": inlets,
        "note": "toy terrain, synthetic rain, single trunk pipe; not the paper's Glasgow block",
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise ValueError(f"无法读取 JSON 输入: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON 输入必须是对象")
    return payload


def _validate(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("source_type") not in {"toy", "synthetic"}:
        raise ValueError("输入必须显式提供 source_type（toy 或 synthetic）")
    dem = np.asarray(data["dem_m"], dtype=float)
    if dem.ndim != 2 or not np.all(np.isfinite(dem)):
        raise ValueError("dem_m 必须是非空且有限的二维数组")
    rain = np.asarray(data["rain_mm"], dtype=float)
    if rain.ndim != 1 or np.any(rain < 0):
        raise ValueError("rain_mm 必须是一维非负序列")
    pipe = data["pipe"]
    n = len(pipe["cells"])
    if len(pipe["invert_m"]) != n or n < 2:
        raise ValueError("pipe.invert_m 长度必须与 pipe.cells 一致且至少两段")
    if float(pipe["area_m2"]) <= 0 or float(pipe["length_m"]) <= 0:
        raise ValueError("pipe.area_m2 与 pipe.length_m 必须为正")
    rows, cols = dem.shape
    for key in ("cells", "manholes", "inlets"):
        items = pipe["cells"] if key == "cells" else data[key]
        for item in items:
            r, c = int(item["row"]), int(item["col"])
            if not (0 <= r < rows and 0 <= c < cols):
                raise ValueError(f"{key} 中的单元落在网格外: ({r},{c})")
    for unit in list(data["manholes"]) + list(data["inlets"]):
        idx = int(unit["pipe_index"])
        if not 0 <= idx < n:
            raise ValueError(f"{unit.get('unit_id')} 的 pipe_index 越界")
        if float(unit["orifice_m2"]) <= 0:
            raise ValueError(f"{unit.get('unit_id')} 的孔口面积必须为正")
    return {
        "dem": dem,
        "rain": rain,
        "dt_s": float(data["dt_s"]),
        "manning_surface": float(data["manning_surface"]),
        "manning_pipe": float(data["manning_pipe"]),
        "pipe_cells": [(int(i["row"]), int(i["col"])) for i in pipe["cells"]],
        "pipe_area": float(pipe["area_m2"]),
        "pipe_len": float(pipe["length_m"]),
        "pipe_invert": np.asarray(pipe["invert_m"], dtype=float),
        "pipe_level0": float(pipe["initial_level_m"]),
        "outfall_index": int(pipe["outfall_index"]),
        "outfall_stage": float(pipe["outfall_stage_m"]),
        "manholes": data["manholes"],
        "inlets": data["inlets"],
        "resolution_m": float(data.get("grid_resolution_m", CELL_M)),
        "source_type": str(data["source_type"]),
    }


def _surface_step(
    depth: np.ndarray, dem: np.ndarray, manning: float, dx: float, sub_dt: float
) -> tuple[np.ndarray, float]:
    """One explicit finite-volume sub-step on the surface; returns new depth and outflow volume."""

    eta = dem + depth
    slope_x = eta[:, :-1] - eta[:, 1:]
    crest_x = np.maximum(dem[:, :-1], dem[:, 1:])
    head_x = np.maximum(0.0, np.maximum(eta[:, :-1], eta[:, 1:]) - crest_x)
    flux_x = np.sign(slope_x) * (1.0 / manning) * head_x ** (5.0 / 3.0) * np.sqrt(np.abs(slope_x) / dx) * dx

    slope_y = eta[:-1, :] - eta[1:, :]
    crest_y = np.maximum(dem[:-1, :], dem[1:, :])
    head_y = np.maximum(0.0, np.maximum(eta[:-1, :], eta[1:, :]) - crest_y)
    flux_y = np.sign(slope_y) * (1.0 / manning) * head_y ** (5.0 / 3.0) * np.sqrt(np.abs(slope_y) / dx) * dx

    give = np.zeros_like(depth)
    give[:, :-1] += np.maximum(0.0, flux_x)
    give[:, 1:] += np.maximum(0.0, -flux_x)
    give[:-1, :] += np.maximum(0.0, flux_y)
    give[1:, :] += np.maximum(0.0, -flux_y)
    available = depth * CELL_AREA_M2 / sub_dt
    scale = np.where(give > 0, np.minimum(1.0, available / np.maximum(give, 1e-12)), 1.0)

    fx = flux_x * np.where(flux_x > 0, scale[:, :-1], scale[:, 1:])
    fy = flux_y * np.where(flux_y > 0, scale[:-1, :], scale[1:, :])

    delta = np.zeros_like(depth)
    delta[:, :-1] -= fx
    delta[:, 1:] += fx
    delta[:-1, :] -= fy
    delta[1:, :] += fy
    new_depth = np.maximum(0.0, depth + delta * sub_dt / CELL_AREA_M2)
    if float((depth + delta * sub_dt / CELL_AREA_M2).min()) < -1e-9:
        raise RuntimeError("地表出现负水深，限流器失效")
    return new_depth, 0.0


def _exchange(
    depth: np.ndarray, dem: np.ndarray, pipe_level: np.ndarray, fixture: dict[str, Any],
    units: list[dict[str, Any]], sub_dt: float, cd: float = 0.62,
) -> tuple[np.ndarray, np.ndarray, float]:
    """Orifice exchange between surface cells and pipe segments; returns updated states and net moved volume."""

    moved = 0.0
    for unit in units:
        r, c = int(unit["row"]), int(unit["col"])
        k = int(unit["pipe_index"])
        h_surface = float(dem[r, c] + depth[r, c])
        h_pipe = float(fixture["pipe_invert"][k] + pipe_level[k])
        dh = h_surface - h_pipe
        if abs(dh) < 1e-9:
            continue
        q = cd * float(unit["orifice_m2"]) * np.sqrt(2.0 * G * abs(dh)) * np.sign(dh)  # m3/s, + 地表→管网
        # 限流：不能抽出超过地表可用水量，也不能从管网取走超过管内存水
        if q > 0:
            q = min(q, float(depth[r, c]) * CELL_AREA_M2 / sub_dt)
        else:
            q = -min(-q, float(pipe_level[k]) * fixture["pipe_area"] / sub_dt)
        if q == 0:
            continue
        depth[r, c] -= q * sub_dt / CELL_AREA_M2
        pipe_level[k] += q * sub_dt / fixture["pipe_area"]
        moved += q * sub_dt
    depth = np.maximum(depth, 0.0)
    pipe_level = np.maximum(pipe_level, 0.0)
    return depth, pipe_level, moved


def _pipe_step(
    pipe_level: np.ndarray, fixture: dict[str, Any], sub_dt: float
) -> tuple[np.ndarray, float]:
    """Route the trunk pipe with a Manning conveyance law; outfall discharges to a fixed stage."""

    n = pipe_level.size
    invert = fixture["pipe_invert"]
    area = fixture["pipe_area"]
    length = fixture["pipe_len"]
    manning = fixture["manning_pipe"]
    radius = float(np.sqrt(area / np.pi))
    hydraulic_radius = radius / 2.0
    conveyance = area * hydraulic_radius ** (2.0 / 3.0) / manning

    face_q = np.zeros(max(n - 1, 0))
    for k in range(n - 1):
        head = (invert[k] + pipe_level[k]) - (invert[k + 1] + pipe_level[k + 1])
        if abs(head) < 1e-9:
            continue
        face_q[k] = conveyance * np.sqrt(abs(head) / length) * np.sign(head)  # m3/s

    out_index = fixture["outfall_index"]
    head_out = (invert[out_index] + pipe_level[out_index]) - fixture["outfall_stage"]
    q_out = conveyance * np.sqrt(head_out / length) if head_out > 0 else 0.0

    # 每个管段可能同时向上下游两个面放水，必须按"总出流"统一缩放，
    # 否则单面限流会让管段给出超过自身存水，水位被钳到 0 时凭空造水。
    wanted = np.zeros(n)
    for k in range(face_q.size):
        donor = k if face_q[k] > 0 else k + 1
        wanted[donor] += abs(face_q[k])
    wanted[out_index] += abs(q_out)
    available = pipe_level * area / sub_dt
    scale = np.where(wanted > 0, np.minimum(1.0, available / np.maximum(wanted, 1e-12)), 1.0)

    delta = np.zeros(n)
    for k in range(face_q.size):
        donor = k if face_q[k] > 0 else k + 1
        q = face_q[k] * scale[donor]
        delta[k] -= q
        delta[k + 1] += q

    q_out *= scale[out_index]
    delta[out_index] -= q_out
    outfall_volume = q_out * sub_dt

    new_level = np.maximum(0.0, pipe_level + delta * sub_dt / area)
    return new_level, outfall_volume


def _full_pipe_spill(
    pipe_level: np.ndarray, depth: np.ndarray, dem: np.ndarray, fixture: dict[str, Any]
) -> tuple[np.ndarray, np.ndarray, float]:
    """满管溢流：管中水位不能超过管径，超出部分从检查井冒到地表。

    不做这一步的话，交换会把地表水一路压进管网、管内水位涨到十几米，
    再反向倒灌回地表，凭空放大体积。
    """

    radius = float(np.sqrt(fixture["pipe_area"] / np.pi))
    diameter = 2.0 * radius
    excess = np.maximum(0.0, pipe_level - diameter)
    pipe_level = np.minimum(pipe_level, diameter)
    spilled = 0.0
    if float(excess.sum()) <= 0.0:
        return pipe_level, depth, spilled

    units = list(fixture["manholes"])
    per_index: dict[int, int] = {}
    for unit in units:
        per_index[int(unit["pipe_index"])] = per_index.get(int(unit["pipe_index"]), 0) + 1
    for unit in units:
        k = int(unit["pipe_index"])
        share = excess[k] * fixture["pipe_area"] / per_index[k]
        if share <= 0:
            continue
        r, c = int(unit["row"]), int(unit["col"])
        depth[r, c] += share / CELL_AREA_M2
        spilled += share
    return pipe_level, depth, spilled


def _run_mode(fixture: dict[str, Any], mode: str) -> dict[str, Any]:
    dem = fixture["dem"]
    rows, cols = dem.shape
    depth = np.zeros((rows, cols))
    pipe_level = np.full(fixture["pipe_invert"].size, fixture["pipe_level0"], dtype=float)
    max_depth = np.zeros((rows, cols))
    sub_dt = fixture["dt_s"] / SUBSTEPS_PER_STEP

    if mode == "none":
        units: list[dict[str, Any]] = []
    elif mode == "manhole":
        units = list(fixture["manholes"])
    elif mode == "inlet":
        units = list(fixture["inlets"])
    elif mode == "inlet_manhole":
        units = list(fixture["inlets"]) + list(fixture["manholes"])
    else:
        raise ValueError(f"未知交换模式: {mode}")

    rain_volume = 0.0
    to_pipe = 0.0
    to_surface = 0.0
    outfall = 0.0
    spill = 0.0
    wet_area = []
    pipe_peak = 0.0

    for step in range(fixture["rain"].size):
        rain_volume += float(fixture["rain"][step]) / 1000.0 * rows * cols * CELL_AREA_M2
        depth += fixture["rain"][step] / 1000.0
        for _ in range(SUBSTEPS_PER_STEP):
            depth, _ = _surface_step(depth, dem, fixture["manning_surface"], fixture["resolution_m"], sub_dt)
            # 双向往返两次，减小显式交换的时间滞后
            for _ in range(2):
                before_surface = float(depth.sum() * CELL_AREA_M2)
                before_pipe = float(pipe_level.sum() * fixture["pipe_area"])
                depth, pipe_level, moved = _exchange(depth, dem, pipe_level, fixture, units, sub_dt)
                if moved > 0:
                    to_pipe += moved
                else:
                    to_surface += -moved
                if abs(
                    (float(depth.sum() * CELL_AREA_M2) + float(pipe_level.sum() * fixture["pipe_area"]))
                    - (before_surface + before_pipe)
                ) > 1e-9:
                    raise RuntimeError("交换步体积不守恒")
            pipe_level, of = _pipe_step(pipe_level, fixture, sub_dt)
            outfall += of
            pipe_level, depth, spilled = _full_pipe_spill(pipe_level, depth, dem, fixture)
            spill += spilled
            max_depth = np.maximum(max_depth, depth)
        wet_area.append(float(np.count_nonzero(depth > 0.3) * CELL_AREA_M2))
        pipe_peak = max(pipe_peak, float(pipe_level.max()))

    surface_storage = float(depth.sum() * CELL_AREA_M2)
    pipe_storage = float(pipe_level.sum() * fixture["pipe_area"])
    residual = rain_volume - surface_storage - pipe_storage - outfall
    return {
        "mode": mode,
        "exchange_unit_count": len(units),
        "conservation": {
            "rain_inflow_m3": rain_volume,
            "surface_storage_m3": surface_storage,
            "pipe_storage_m3": pipe_storage,
            "outfall_m3": outfall,
            "surface_to_pipe_m3": to_pipe,
            "pipe_to_surface_m3": to_surface,
            "pipe_full_spill_m3": spill,
            "volume_residual_m3": residual,
            "relative_residual": residual / max(rain_volume, 1e-12),
        },
        "stats": {
            "max_depth_m": float(max_depth.max()),
            "final_wet_area_gt_030cm_m2": wet_area[-1],
            "max_wet_area_gt_030cm_m2": float(np.max(wet_area)),
            "peak_pipe_level_m": pipe_peak,
        },
        "_max_depth": max_depth,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="1D-2D bidirectional coupling smoke with three exchange modes")
    parser.add_argument("--source", choices=("synthetic", "json"), default="synthetic")
    parser.add_argument("--input", type=Path)
    parser.add_argument("--modes", default=",".join(MODES))
    parser.add_argument("--output-dir", type=Path, default=Path(__file__).parent / "results")
    args = parser.parse_args()

    if args.source == "json":
        if args.input is None:
            parser.error("--source json 必须同时提供 --input")
        data = _read_json(args.input)
    else:
        if args.input is not None:
            parser.error("--source synthetic 不接受 --input")
        data = _synthetic_fixture()

    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    unknown = [m for m in modes if m not in MODES]
    if unknown:
        print(f"未知交换模式：{unknown}", file=sys.stderr)
        return 1

    try:
        fixture = _validate(data)
    except (ValueError, KeyError, TypeError) as exc:
        print(f"输入校验失败：{exc}", file=sys.stderr)
        return 1

    runs: dict[str, dict[str, Any]] = {}
    arrays: dict[str, np.ndarray] = {}
    for mode in modes:
        result = _run_mode(fixture, mode)
        rel = abs(result["conservation"]["relative_residual"])
        if rel > 1e-9:
            print(f"模式 {mode} 体积不守恒：相对残差 {rel:.3e}", file=sys.stderr)
            return 1
        arrays[f"max_depth_{mode}"] = result.pop("_max_depth")
        runs[mode] = result

    base_stats = runs.get("none", {}).get("stats", {})
    baseline_area = base_stats.get("max_wet_area_gt_030cm_m2")
    baseline_depth = base_stats.get("max_depth_m")
    comparison = {}
    for mode, result in runs.items():
        area = result["stats"]["max_wet_area_gt_030cm_m2"]
        peak = result["stats"]["max_depth_m"]
        comparison[mode] = {
            "max_wet_area_gt_030cm_m2": area,
            "area_reduction_vs_no_drainage_pct": None
            if not baseline_area
            else round((1.0 - area / baseline_area) * 100.0, 3),
            # 面积按格计，量化到一格；峰值水深是连续量，方向性更稳
            "max_depth_m": peak,
            "depth_reduction_vs_no_drainage_pct": None
            if not baseline_depth
            else round((1.0 - peak / baseline_depth) * 100.0, 3),
        }

    payload = {
        "source_type": fixture["source_type"],
        "paper_result_claim": False,
        "stage": "two_way_1d_2d_coupling",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "command": " ".join(sys.argv),
        "input_sha256": hashlib.sha256(_canonical_json(data)).hexdigest(),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "numpy": np.__version__,
        },
        "method": {
            "name": "1D pipe chain + 2D surface, bidirectional orifice exchange",
            "grid_resolution_m": fixture["resolution_m"],
            "grid_shape": list(fixture["dem"].shape),
            "pipe_segments": int(fixture["pipe_invert"].size),
            "manhole_units": len(fixture["manholes"]),
            "inlet_units": len(fixture["inlets"]),
            "time_steps": int(fixture["rain"].size),
            "dt_s": fixture["dt_s"],
            "substeps_per_step": SUBSTEPS_PER_STEP,
            "exchange_iterations_per_substep": 2,
            "scheme": "explicit finite volume surface + Manning pipe conveyance + orifice exchange",
        },
        "runs": runs,
        "comparison": comparison,
        "limitations": {
            "area_quantization_m2": CELL_AREA_M2,
            "note": (
                "淹没面积按整格计数，最小步长就是一格（100 m²），所以面积百分比是粗粒度的；"
                "峰值水深是连续量，方向性更可靠。检查井 8 个、雨水口 3 个且孔口面积不同，"
                "三者之间的排序取决于本 toy 的布点，不代表论文的 9.3% / 23.2% / 24.5%。"
            ),
        },
        "evidence_boundary": (
            "toy terrain, synthetic rain and a single trunk pipe; no SWMM network, no shallow-water solver, "
            "no Glasgow geometry or observation. Percentages here are properties of this toy setup and must "
            "not be compared with the paper's 9.3% / 23.2% / 24.5%."
        ),
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    np.savez_compressed(args.output_dir / "coupling_outputs.npz", **arrays)
    payload["outputs"] = {"arrays": "coupling_outputs.npz"}
    (args.output_dir / "run.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(comparison, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
