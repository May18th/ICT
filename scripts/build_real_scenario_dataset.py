"""生成"真实暴雨"演示情景覆盖目录（samples/scenarios/real_20260801/）。

做什么
------
把演示数据集（samples/assets/urban）整目录复制，只换装 weather_alerts.csv 里的
**active 橙色行** → 真实暴雨映射行（Open-Meteo/ERA5，2026-08-01 成都过程）：

- 保留真实数字：3h 最大 42.1mm（24h 77.7mm、峰值 15.5mm/h，见 mapping_report.md）
- 演示映射：真实雨量 + 演示事件时间窗（2026-10-29，与冻结工单台账对齐）+ 片区改 A-03
- 已解除行 / 未来红色行原样保留（过期证据测试依赖）

边界（必须诚实）
----------------
- 默认推演口径（assets/urban）一个字节不动；本目录只被 `?scenario=real_20260801` 显式装载
- 预警等级按真实雨量判级（黄色）：橙色响应规则（需预警 orange/red）不触发，
  引擎将显式给出「无匹配等级 · 需人工复核」——这是设计行为，不是缺陷
- 重跑本脚本必须幂等（内容完全一致），守卫测试会钉住这一点
"""

from __future__ import annotations

import csv
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "samples" / "assets" / "urban"
TARGET_DIR = ROOT / "samples" / "scenarios" / "real_20260801"
VARIANT_REPORT = ROOT / "samples" / "weather" / "scenario_variant_20260801"

# 真实暴雨的演示映射值（来源：weather_variant 的 mapping_report.md，勿手改——
# 改映射先改 prepare_weather/weather_to_scenario 的输出，再同步这里）
REAL_STORM = {
    "alert_id": "WA-REAL-20260801-01",
    "level": "yellow",
    "rainfall_mm_3h": "42.1",
    "rainfall_24h_mm": 77.7,
    "peak_mm_h": 15.5,
    "real_date": "2026-08-01",
    # 演示事件时间窗（与被替换的合成橙色行窗口一致，保证工单/资源台账对齐）
    "issued_at": "2026-10-29T07:30:00+08:00",
    "valid_from": "2026-10-29T08:00:00+08:00",
    "valid_to": "2026-10-29T14:00:00+08:00",
    "district": "A-03",
}

README = """# real_20260801 情景覆盖目录（生成物，勿手改）

由 `scripts/build_real_scenario_dataset.py` 生成：samples/assets/urban 全量复制，
仅 weather_alerts.csv 的 active 橙色行换装为真实暴雨映射行。

- 真实数据：Open-Meteo/ERA5（CC BY 4.0），成都 30.66N/104.07E，2026-08-01 过程
  24h 累计 {rain_24h}mm、3h 最大 {rain_3h}mm、峰值 {peak}mm/h
- 演示映射：真实雨量值 + 演示事件时间窗（2026-10-29，与冻结工单台账对齐）
- 预期行为：预警黄色 → 橙色响应规则（需预警 orange/red）不触发 →
  引擎显式输出「无匹配等级 · 需人工复核」，不硬凑等级
- 装载方式：GET /api/demo?scenario=real_20260801（白名单校验，默认口径不变）
- 重算：`python scripts/build_real_scenario_dataset.py`（幂等）

映射方法与真实雨量推导见 `samples/weather/scenario_variant_20260801/mapping_report.md`。
"""


def build_weather_csv() -> str:
    """读原始 weather_alerts.csv，仅替换 active 橙色行为真实映射行。"""
    src = SOURCE_DIR / "weather_alerts.csv"
    rows: list[dict[str, str]] = []
    with src.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        fieldnames = list(reader.fieldnames or [])
        rows.extend(reader)

    replaced = 0
    for row in rows:
        # 精确替换演示窗口内的 active 橙色行；红色行（10-30，未来窗口）原样保留
        if row.get("alert_id", "").strip() == "WA-20261029-01":
            row.update(
                {
                    "alert_id": REAL_STORM["alert_id"],
                    "issued_at": REAL_STORM["issued_at"],
                    "valid_from": REAL_STORM["valid_from"],
                    "valid_to": REAL_STORM["valid_to"],
                    "level": REAL_STORM["level"],
                    "rainfall_mm_3h": REAL_STORM["rainfall_mm_3h"],
                    "district": REAL_STORM["district"],
                    "status": "active",
                    "description": (
                        f"真实暴雨映射（Open-Meteo/ERA5，{REAL_STORM['real_date']} 成都）："
                        f"24h 累计 {REAL_STORM['rainfall_24h_mm']}mm，3h 最大 "
                        f"{REAL_STORM['rainfall_mm_3h']}mm，峰值小时 {REAL_STORM['peak_mm_h']}mm；"
                        "演示映射：真实雨量 + 演示事件时间窗，映射规则见 "
                        "samples/weather/scenario_variant_20260801/mapping_report.md"
                    ),
                }
            )
            replaced += 1

    if replaced != 1:
        raise RuntimeError(f"期望恰好替换 WA-20261029-01 一行，实际 {replaced} 行")

    import io

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


def main() -> None:
    if TARGET_DIR.exists():
        shutil.rmtree(TARGET_DIR)
    TARGET_DIR.mkdir(parents=True)
    for item in sorted(SOURCE_DIR.iterdir()):
        if item.is_file():
            shutil.copy2(item, TARGET_DIR / item.name)

    (TARGET_DIR / "weather_alerts.csv").write_text(
        build_weather_csv(), encoding="utf-8"
    )
    (TARGET_DIR / "README.md").write_text(
        README.format(
            rain_24h=REAL_STORM["rainfall_24h_mm"],
            rain_3h=REAL_STORM["rainfall_mm_3h"],
            peak=REAL_STORM["peak_mm_h"],
        ),
        encoding="utf-8",
    )
    print(f"overlay -> {TARGET_DIR} ({len(list(TARGET_DIR.iterdir()))} files)")


if __name__ == "__main__":
    main()
