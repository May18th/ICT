# -*- coding: utf-8 -*-
"""钉住 weather_to_scenario 的关键输出，防止真实数据/映射规则被无声改动。

数字全部锚定 sources of truth：
- 输入：samples/weather/chengdu_hourly_2026-06-01_2026-09-30.csv（Open-Meteo/ERA5）
- 判定规则：samples/scenarios/vd02/plan_flood_emergency_cd_2026.md 第 11-16 行
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "weather_to_scenario.py"
OUT = ROOT / "samples" / "weather" / "scenario_variant_20260801"


def _ensure_generated() -> None:
    if not (OUT / "mapping_report.md").exists():
        subprocess.run([sys.executable, str(SCRIPT)], check=True, cwd=ROOT)


def test_variant_files_exist() -> None:
    _ensure_generated()
    for name in ("rainfall_window.csv", "weather_alerts_real.csv", "mapping_report.md"):
        assert (OUT / name).is_file(), name


def test_window_totals_match_real_data() -> None:
    _ensure_generated()
    report = (OUT / "mapping_report.md").read_text(encoding="utf-8")
    # 汛期最大 24h 过程（与 prepare_weather.py 的结论一致）
    assert "77.7 mm" in report
    assert "| 6h | 62.6 mm |" in report
    assert "| 12h | 72.8 mm |" in report
    assert "峰值小时：15.5 mm" in report


def test_response_level_is_level4_blue() -> None:
    """6h 62.6mm ≥ 50mm → 四级响应（蓝色）；12h 72.8mm < 100mm 不升三级。"""
    _ensure_generated()
    report = (OUT / "mapping_report.md").read_text(encoding="utf-8")
    assert "**判定结果：四级响应（蓝色）**" in report
    assert "**判定结果：三级响应" not in report


def test_alert_row_is_consistent_with_vd02_schema() -> None:
    """预警行字段必须与 vd02/weather_alerts.csv 完全同构，演示可直接换装。"""
    _ensure_generated()
    src_cols = (
        (ROOT / "samples/scenarios/vd02/weather_alerts.csv").read_text(encoding="utf-8-sig")
        .splitlines()[0].lstrip("﻿").split(",")
    )
    new_cols = (
        (OUT / "weather_alerts_real.csv").read_text(encoding="utf-8-sig")
        .splitlines()[0].lstrip("﻿").split(",")
    )
    assert new_cols == src_cols, (new_cols, src_cols)
    # 原情景不受影响
    vd02 = (ROOT / "samples/scenarios/vd02/weather_alerts.csv").read_text(encoding="utf-8")
    assert "WA-V2-20261101-01" in vd02
    assert "WA-REAL-20260801-01" not in vd02
