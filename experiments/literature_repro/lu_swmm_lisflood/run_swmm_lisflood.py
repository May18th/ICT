"""卢兴超 2024 SWMM-LISFLOOD-FP 的"管网溢流 → 地表漫流"单向耦合 smoke。

脚本只实现可审计的最小单向耦合适配器：一维管网侧的节点溢流过程作为地表二维模型的
入流边界，二维模型负责把水摊开并给出淹没深度与淹没面积。它不实现 SWMM 管网求解器，
也不实现 LISFLOOD-FP 的完整浅水格式，更不使用论文的 DEM、糙率、管网与观测数据，
因此输出只能标 ``toy``，不能当作论文的 NSE 或淹没范围结果。
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
DEPTH_THRESHOLDS_M = (0.3, 1.0)
SUBSTEPS_PER_STEP = 60


def _canonical_json(value: Any) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _synthetic_fixture(seed: int = 20261122) -> dict[str, Any]:
    """Deterministic toy terrain plus a set of node overflow hydrographs.

    Terrain is a tilted plane with one bowl-shaped depression; six node cells carry
    overflow hydrographs with different peaks and arrival times so the surface model
    has something non-uniform to route.
    """

    rng = np.random.default_rng(seed)
    rows, cols = 18, 24
    yy, xx = np.mgrid[0:rows, 0:cols]
    base = 8.0 - 0.05 * yy - 0.02 * xx
    bowl_r = np.hypot((yy - 11.0) * 0.9, (xx - 12.0) * 0.7)
    bowl = -0.9 * np.exp(-(bowl_r**2) / 12.0)
    dem = np.round(base + bowl + rng.normal(0.0, 0.01, size=(rows, cols)), 4)

    nodes = [
        {"node_id": "N-01", "row": 11, "col": 12},
        {"node_id": "N-02", "row": 10, "col": 13},
        {"node_id": "N-03", "row": 12, "col": 11},
        {"node_id": "N-04", "row": 9, "col": 12},
        {"node_id": "N-05", "row": 11, "col": 14},
        {"node_id": "N-06", "row": 13, "col": 13},
    ]
    steps, dt_s = 12, 300.0
    peaks = np.array([0.85, 0.62, 0.48, 0.35, 0.28, 0.21])
    delays = np.array([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
    overflow = np.zeros((steps, len(nodes)), dtype=float)
    t = np.arange(steps, dtype=float)
    for i in range(len(nodes)):
        shifted = np.clip((t - delays[i]) * dt_s / 1800.0, 0.0, None)
        overflow[:, i] = np.round(peaks[i] * np.exp(-((shifted - 1.0) ** 2) / 0.35), 4)

    return {
        "source_type": "toy",
        "grid_resolution_m": CELL_M,
        "dem_m": dem.tolist(),
        "manning_n": 0.03,
        "dt_s": dt_s,
        "overflow_m3s": overflow.tolist(),
        "nodes": nodes,
        "boundary_stage_m": 0.0,
        "note": "toy terrain and synthetic overflow hydrographs; not the paper's DEM or pipe network",
    }


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - 明确报出读取失败原因
        raise ValueError(f"无法读取 JSON 输入: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise ValueError("JSON 输入必须是对象")
    return payload


def _validate(data: dict[str, Any]) -> dict[str, Any]:
    if data.get("source_type") not in {"toy", "synthetic"}:
        raise ValueError("输入必须显式提供 source_type（toy 或 synthetic）")
    dem = np.asarray(data["dem_m"], dtype=float)
    if dem.ndim != 2 or dem.size == 0:
        raise ValueError("dem_m 必须是非空二维数组")
    if not np.all(np.isfinite(dem)):
        raise ValueError("dem_m 含非有限值")
    dt_s = float(data["dt_s"])
    if dt_s <= 0:
        raise ValueError("dt_s 必须为正")
    manning = float(data["manning_n"])
    if not 0.005 <= manning <= 0.2:
        raise ValueError("manning_n 超出合理范围（0.005–0.2）")
    nodes = data["nodes"]
    overflow = np.asarray(data["overflow_m3s"], dtype=float)
    if overflow.ndim != 2 or overflow.shape[1] != len(nodes):
        raise ValueError("overflow_m3s 形状必须是 (时间步, 节点数)")
    if np.any(overflow < 0):
        raise ValueError("overflow_m3s 不能为负")
    rows, cols = dem.shape
    for node in nodes:
        r, c = int(node["row"]), int(node["col"])
        if not (0 <= r < rows and 0 <= c < cols):
            raise ValueError(f"节点 {node.get('node_id')} 落在网格外")
    return {
        "dem": dem,
        "manning": manning,
        "dt_s": dt_s,
        "overflow": overflow,
        "nodes": nodes,
        "boundary_stage": float(data.get("boundary_stage_m", 0.0)),
        "resolution_m": float(data.get("grid_resolution_m", CELL_M)),
        "source_type": str(data["source_type"]),
    }


def _face_flux(eta: np.ndarray, z: np.ndarray, manning: float, dx: float) -> tuple[np.ndarray, np.ndarray]:
    """Manning-type flux across interior faces (m3/s), positive pointing down-gradient.

    Overflow depth is measured above the higher of the two cell beds (堰顶), using the
    higher water surface as the driving stage. Using ``min(eta)`` instead would return
    zero flux into a dry neighbour, which stops the water from ever spreading.
    """

    slope_x = eta[:, :-1] - eta[:, 1:]
    crest_x = np.maximum(z[:, :-1], z[:, 1:])
    head_x = np.maximum(0.0, np.maximum(eta[:, :-1], eta[:, 1:]) - crest_x)
    flux_x = np.sign(slope_x) * (1.0 / manning) * head_x ** (5.0 / 3.0) * np.sqrt(np.abs(slope_x) / dx) * dx

    slope_y = eta[:-1, :] - eta[1:, :]
    crest_y = np.maximum(z[:-1, :], z[1:, :])
    head_y = np.maximum(0.0, np.maximum(eta[:-1, :], eta[1:, :]) - crest_y)
    flux_y = np.sign(slope_y) * (1.0 / manning) * head_y ** (5.0 / 3.0) * np.sqrt(np.abs(slope_y) / dx) * dx
    return flux_x, flux_y


def _edge_flux(eta_edge: np.ndarray, stage: float, manning: float, dx: float) -> np.ndarray:
    """Free-outfall flux at the four grid edges (m3/s)."""

    head = np.maximum(0.0, eta_edge - stage)
    return (1.0 / manning) * head ** (5.0 / 3.0) * np.sqrt(1.0 / dx) * dx


def _route_surface(
    dem: np.ndarray,
    manning: float,
    dt_s: float,
    overflow: np.ndarray,
    nodes: list[dict[str, Any]],
    boundary_stage: float,
    dx: float,
    substeps: int = SUBSTEPS_PER_STEP,
) -> dict[str, Any]:
    """Explicit finite-volume routing with a donor-volume limiter.

    Each face flux leaves one cell and enters its neighbour, so the interior scheme is
    volume-conserving by construction. Fluxes are capped by the donor's available water
    in that sub-step, which keeps depth non-negative without an implicit solve.
    """

    rows, cols = dem.shape
    depth = np.zeros((rows, cols), dtype=float)
    max_depth = np.zeros((rows, cols), dtype=float)
    wet_area = np.zeros(overflow.shape[0], dtype=float)
    inflow_total = 0.0
    outflow_total = 0.0
    sub_dt = dt_s / substeps

    node_cells = [(int(node["row"]), int(node["col"])) for node in nodes]

    for step in range(overflow.shape[0]):
        step_volume = 0.0
        for idx, (r, c) in enumerate(node_cells):
            node_volume = float(overflow[step, idx]) * dt_s
            step_volume += node_volume
            depth[r, c] += node_volume / CELL_AREA_M2
        inflow_total += step_volume

        for _ in range(substeps):
            eta = dem + depth
            flux_x, flux_y = _face_flux(eta, dem, manning, dx)
            q_left = _edge_flux(eta[:, 0], boundary_stage, manning, dx)
            q_right = _edge_flux(eta[:, -1], boundary_stage, manning, dx)
            q_top = _edge_flux(eta[0, :], boundary_stage, manning, dx)
            q_bottom = _edge_flux(eta[-1, :], boundary_stage, manning, dx)

            give = np.zeros_like(depth)
            give[:, :-1] += np.maximum(0.0, flux_x)
            give[:, 1:] += np.maximum(0.0, -flux_x)
            give[:-1, :] += np.maximum(0.0, flux_y)
            give[1:, :] += np.maximum(0.0, -flux_y)
            give[:, 0] += q_left
            give[:, -1] += q_right
            give[0, :] += q_top
            give[-1, :] += q_bottom

            available = depth * CELL_AREA_M2 / sub_dt
            scale = np.where(give > 0, np.minimum(1.0, available / np.maximum(give, 1e-12)), 1.0)

            # 限流只约束给水方（donor）：受体上游是否有水与它能否收水无关
            fx = flux_x * np.where(flux_x > 0, scale[:, :-1], scale[:, 1:])
            fy = flux_y * np.where(flux_y > 0, scale[:-1, :], scale[1:, :])

            delta = np.zeros_like(depth)
            delta[:, :-1] -= fx
            delta[:, 1:] += fx
            delta[:-1, :] -= fy
            delta[1:, :] += fy
            delta[:, 0] -= q_left * scale[:, 0]
            delta[:, -1] -= q_right * scale[:, -1]
            delta[0, :] -= q_top * scale[0, :]
            delta[-1, :] -= q_bottom * scale[-1, :]

            depth = depth + delta * sub_dt / CELL_AREA_M2
            if float(depth.min()) < -1e-9:
                raise RuntimeError(f"出现负水深 {float(depth.min()):.3e} m，限流器失效")
            depth = np.maximum(depth, 0.0)
            outflow_total += sub_dt * (
                float((q_left * scale[:, 0]).sum())
                + float((q_right * scale[:, -1]).sum())
                + float((q_top * scale[0, :]).sum())
                + float((q_bottom * scale[-1, :]).sum())
            )
            max_depth = np.maximum(max_depth, depth)
        wet_area[step] = float(np.count_nonzero(depth > 1e-3) * CELL_AREA_M2)

    storage = float(depth.sum() * CELL_AREA_M2)
    areas = {
        f"inundation_area_gt_{int(th * 100):03d}cm_m2": float(np.count_nonzero(depth > th) * CELL_AREA_M2)
        for th in DEPTH_THRESHOLDS_M
    }
    return {
        "depth_m": depth,
        "max_depth_m": max_depth,
        "conservation": {
            "inflow_m3": inflow_total,
            "boundary_outflow_m3": outflow_total,
            "storage_m3": storage,
            "volume_residual_m3": inflow_total - outflow_total - storage,
            "relative_residual": (inflow_total - outflow_total - storage) / max(inflow_total, 1e-12),
        },
        "stats": {
            "max_depth_m": float(max_depth.max()),
            "max_wet_area_m2": float(wet_area.max()),
            "final_wet_area_m2": float(wet_area[-1]),
            "peak_overflow_m3s": float(overflow.max()),
            "overflow_volume_m3": float(inflow_total),
            "substeps_per_step": substeps,
            **areas,
        },
    }


def _write_result(output_dir: Path, result: dict[str, Any], arrays: dict[str, np.ndarray]) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    arrays_path = output_dir / "surface_outputs.npz"
    np.savez_compressed(arrays_path, **arrays)
    result["outputs"] = {"arrays": arrays_path.name}
    (output_dir / "run.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="SWMM overflow to 2D surface one-way coupling smoke")
    parser.add_argument("--source", choices=("synthetic", "json"), default="synthetic")
    parser.add_argument("--input", type=Path)
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

    try:
        fixture = _validate(data)
    except (ValueError, KeyError, TypeError) as exc:
        print(f"输入校验失败：{exc}", file=sys.stderr)
        return 1

    routed = _route_surface(
        fixture["dem"],
        fixture["manning"],
        fixture["dt_s"],
        fixture["overflow"],
        fixture["nodes"],
        fixture["boundary_stage"],
        fixture["resolution_m"],
    )
    conservation = routed["conservation"]
    if abs(conservation["relative_residual"]) > 1e-6:
        print(f"体积不守恒：相对残差 {conservation['relative_residual']:.3e}", file=sys.stderr)
        return 1

    result = {
        "source_type": fixture["source_type"],
        "paper_result_claim": False,
        "stage": "one_way_overflow_to_surface",
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "command": " ".join(sys.argv),
        "input_sha256": hashlib.sha256(_canonical_json(data)).hexdigest(),
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "numpy": np.__version__,
        },
        "method": {
            "name": "pipe overflow to 2D surface, one-way",
            "grid_resolution_m": fixture["resolution_m"],
            "grid_shape": list(fixture["dem"].shape),
            "node_count": len(fixture["nodes"]),
            "time_steps": int(fixture["overflow"].shape[0]),
            "dt_s": fixture["dt_s"],
            "manning_n": fixture["manning"],
            "scheme": "explicit finite volume, Manning face flux, donor-volume limiter, free outfall",
        },
        "conservation": conservation,
        "stats": routed["stats"],
        "evidence_boundary": (
            "toy DEM and synthetic overflow hydrographs; no SWMM solver, no LISFLOOD-FP shallow-water "
            "scheme, no calibration or observation. Not comparable with the paper's NSE or inundation maps."
        ),
    }
    _write_result(
        args.output_dir,
        result,
        {"depth_m": routed["depth_m"], "max_depth_m": routed["max_depth_m"]},
    )
    print(json.dumps({"stats": routed["stats"], "conservation": conservation}, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
