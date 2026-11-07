"""情景切换闭环守卫：真实暴雨变体装载、降雨回放、SWMM 参照端点。

钉的是行为不变量，不是具体展示文案：
- 默认口径（合成橙色）一个字节不动——冻结数字的根
- ?scenario=real_20260801 白名单装载，warning_level 变黄、结论仍由实测深度驱动（OR 规则）
- 未知情景 400
- 三个端点的形状与关键数字（与生成器产物同源，防无声漂移）
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from decision_poc.web_demo import route

ROOT = Path(__file__).resolve().parents[1]


def _payload(path: str) -> dict:
    status, _, body = route(path)
    assert status == 200, f"{path} -> {status}"
    return json.loads(body)


@pytest.fixture(scope="module")
def default_payload() -> dict:
    return _payload("/api/demo")


@pytest.fixture(scope="module")
def real_payload() -> dict:
    return _payload("/api/demo?scenario=real_20260801")


def test_default_scenario_untouched(default_payload: dict) -> None:
    """默认口径必须是冻结的合成橙色情景，任何切换功能不得改变它。"""
    scenario = default_payload["scenario"]
    assert scenario["id"] == "synthetic_v1"
    assert scenario["weather_provenance"] == "synthetic"
    assert default_payload["facts"]["response_level"] == "orange"
    assert default_payload["facts"]["warning_level"] == "orange"
    assert default_payload["publishable"] is True


def test_real_scenario_loads_and_swaps_warning(real_payload: dict) -> None:
    """真实情景：预警佐证变黄（ERA5 映射行），响应等级仍由实测深度驱动（OR 规则）。"""
    scenario = real_payload["scenario"]
    assert scenario["id"] == "real_20260801"
    assert scenario["weather_provenance"] == "real"
    assert "Open-Meteo" in scenario["weather_source"]
    assert real_payload["facts"]["warning_level"] == "yellow"
    # OR 规则：depth ≥ 25cm 单独足以定级，数据源更换不得摆动结论
    assert real_payload["facts"]["response_level"] == "orange"
    assert real_payload["publishable"] is True


def test_real_scenario_dataset_matches_default_except_weather() -> None:
    """覆盖目录除 weather_alerts.csv/README.md 外必须与默认数据集逐字节一致。"""
    import filecmp

    src = ROOT / "samples" / "assets" / "urban"
    dst = ROOT / "samples" / "scenarios" / "real_20260801"
    names = {p.name for p in src.iterdir() if p.is_file()}
    assert names == {p.name for p in dst.iterdir() if p.is_file()}
    for name in sorted(names):
        if name in {"weather_alerts.csv", "README.md"}:
            continue
        assert filecmp.cmp(src / name, dst / name, shallow=False), name


def test_real_weather_row_swaps_only_the_demo_alert() -> None:
    """换装只动 WA-20261029-01 一行：过期行/未来红色行原样保留。"""
    import csv

    src_rows = list(
        csv.DictReader(
            (ROOT / "samples" / "assets" / "urban" / "weather_alerts.csv").open(
                encoding="utf-8-sig"
            )
        )
    )
    dst_rows = list(
        csv.DictReader(
            (ROOT / "samples" / "scenarios" / "real_20260801" / "weather_alerts.csv").open(
                encoding="utf-8-sig"
            )
        )
    )
    by_id_src = {r["alert_id"]: r for r in src_rows}
    by_id_dst = {r["alert_id"]: r for r in dst_rows}
    # 换装行 ID 改为 WA-REAL-20260801-01（保留真实日期标识），其余行集合不变
    assert by_id_src.keys() - {"WA-20261029-01"} == by_id_dst.keys() - {
        "WA-REAL-20260801-01"
    }, "除换装行外行集合必须不变"
    assert "WA-REAL-20260801-01" in by_id_dst
    changed = {
        aid
        for aid in by_id_src
        if aid in by_id_dst
        and json.dumps(by_id_src[aid], sort_keys=True)
        != json.dumps(by_id_dst[aid], sort_keys=True)
    }
    assert changed == set(), "未换装的行不许有任何字段漂移"
    real = by_id_dst["WA-REAL-20260801-01"]
    assert real["level"] == "yellow"
    assert real["rainfall_mm_3h"] == "42.1"
    assert real["district"] == "A-03"


def test_unknown_scenario_rejected() -> None:
    status, _, _ = route("/api/demo?scenario=../etc")
    assert status == 400


def test_weather_replay_shape() -> None:
    data = _payload("/api/weather-replay")
    assert data["date"] == "2026-08-01"
    assert len(data["points"]) == 24
    assert data["peak_mm"] == 15.5
    assert data["peak_time"] == "05:00"
    # 自然日累计（77.5）与滑动窗口口径（77.7）必须在注记里说清，防止材料追问
    assert abs(data["total_mm"] - 77.5) < 0.05
    assert "滑动窗口" in data["note"]
    cum = [p["cum_mm"] for p in data["points"]]
    assert cum == sorted(cum), "累计序列必须单调不减"
    assert abs(cum[-1] - data["total_mm"]) < 0.05


def test_quant_references_match_generated_data() -> None:
    """端点数字必须与 swmm_bypass.py 产物逐字段一致（防前端硬编码漂移）。"""
    endpoint = _payload("/api/quant-references")
    source = json.loads(
        (ROOT / "docs" / "reports" / "swmm-bypass-comparison.json").read_text(
            encoding="utf-8"
        )
    )
    assert endpoint == source
    assert endpoint["runoff"]["ratio"] == 1.281
    assert "不约束" in endpoint["trunk"]["verdict"]
