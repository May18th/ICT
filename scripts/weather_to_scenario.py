# -*- coding: utf-8 -*-
"""weather_to_scenario: 把真实暴雨过程映射为 V2 片区推演情景的气象输入。

输入（真实，来源见 samples/weather/README.md）：
    samples/weather/chengdu_hourly_2026-06-01_2026-09-30.csv

输出（可复算）：
    samples/weather/scenario_variant_20260801/
    ├── rainfall_window.csv        # 暴雨窗口逐小时序列（真实原值，未改动）
    ├── weather_alerts_real.csv    # 映射出的气象预警行（与 vd02/weather_alerts.csv 同构）
    └── mapping_report.md          # 逐级推导：累计雨量 → 预警等级 → 响应等级（引用预案原文行号）

红线：vd02 原有情景（WA-V2-20261101-01，3h 60mm）不变；本变体是"如果 2026-08-01
真实暴雨落在 V2 片区"的并行输入，不声称取代原情景，也不回写主链路数据。
"""
import csv
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(r"D:\华为ICT\agent_poc\samples\weather")
SRC = ROOT / "chengdu_hourly_2026-06-01_2026-09-30.csv"
OUT = ROOT / "scenario_variant_20260801"

WINDOW_START = datetime.fromisoformat("2026-08-01T01:00:00")  # CSV 为 Asia/Shanghai 本地时间（naive）
WINDOW_HOURS = 24          # 2026-08-01 01:00 ~ 08-02 00:00，prepare_weather 已确认为汛期最大 24h

# 预警等级映射（引用 samples/scenarios/vd02/plan_flood_emergency_cd_2026.md 第 11-16 行，成都市 2026 预案附件5）
RESPONSE_RULES = [
    ("一级响应（红色）", "24 小时普降 250 毫米以上特大暴雨", 24, 250.0),
    ("二级响应（橙色）", "24 小时普降 150 毫米以上大暴雨", 24, 150.0),
    ("三级响应（黄色）", "12 小时普降 100 毫米以上大暴雨", 12, 100.0),
    ("四级响应（蓝色）", "6 小时普降 50 毫米以上暴雨", 6, 50.0),
]


def load_window() -> list[tuple[datetime, float]]:
    rows = []
    with SRC.open(encoding="utf-8-sig") as f:
        for r in csv.DictReader(f):
            t = datetime.fromisoformat(r["time"])
            if WINDOW_START <= t < WINDOW_START + timedelta(hours=WINDOW_HOURS):
                rows.append((t, float(r["precipitation_mm"])))
    if len(rows) != WINDOW_HOURS:
        raise SystemExit(f"window points {len(rows)} != {WINDOW_HOURS}")
    return rows


def max_cumsum(hours: list[float], span: int) -> tuple[float, int]:
    """滑动 span 小时最大累计，返回 (累计, 起始下标)。"""
    cum = [0.0]
    for v in hours:
        cum.append(cum[-1] + v)
    best_i, best_s = 0, -1.0
    for i in range(len(hours) - span + 1):
        s = cum[i + span] - cum[i]
        if s > best_s:
            best_s, best_i = s, i
    return round(best_s, 1), best_i


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    rows = load_window()
    times = [t for t, _ in rows]
    vals = [v for _, v in rows]

    # 1) 窗口逐小时序列（真实原值）
    with (OUT / "rainfall_window.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["time", "precipitation_mm"])
        for t, v in rows:
            w.writerow([t.isoformat(), v])

    # 2) 各时长最大累计
    stats = {}
    for span in (3, 6, 12, 24):
        s, i = max_cumsum(vals, span)
        stats[span] = (s, times[i])
    peak_i = max(range(len(vals)), key=lambda i: vals[i])

    # 3) 响应等级判定：从高到低第一条满足的规则（数字全部来自本窗口实算）
    hit = None
    for level, desc, span, threshold in RESPONSE_RULES:
        s, _ = stats[span]
        if s >= threshold:
            hit = (level, desc, span, threshold, s)
            break

    # 4) 气象预警行（与 vd02/weather_alerts.csv 字段同构；等级取低于响应等级一档的预警口径，
    #    即按预案"四级响应（蓝色）= 市气象台发布暴雨橙色预警"的本地耦合，这里以实算 3h 雨量定预警色）
    mm3 = stats[3][0]
    if mm3 >= 100:
        alert_level = "red"
    elif mm3 >= 60:
        alert_level = "orange"
    elif mm3 >= 20:
        alert_level = "yellow"
    else:
        alert_level = "blue"

    alert = {
        "alert_id": "WA-REAL-20260801-01",
        "issued_at": times[0].isoformat() + "+08:00",
        "valid_from": times[0].isoformat() + "+08:00",
        "valid_to": (times[-1] + timedelta(hours=1)).isoformat() + "+08:00",
        "level": alert_level,
        "rainfall_mm_3h": mm3,
        "district": "V2-A",
        "source_org": "ORG-METEO",
        "status": "active",
        "description": (
            f"真实暴雨过程（Open-Meteo/ERA5，成都 30.66N 104.07E）映射：24h 累计 "
            f"{stats[24][0]}mm，峰值小时 {vals[peak_i]}mm（{times[peak_i].strftime('%H:%M')}），"
            f"3h 最大 {mm3}mm。"
        ),
    }
    with (OUT / "weather_alerts_real.csv").open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=list(alert))
        w.writeheader()
        w.writerow(alert)

    # 5) 推导报告
    lines = [
        "# 真实暴雨 → 推演情景映射报告（2026-08-01 过程）",
        "",
        f"- 数据：`samples/weather/{SRC.name}`（Open-Meteo / ERA5，CC BY 4.0）",
        f"- 窗口：{times[0].isoformat()} ~ {times[-1].isoformat()}（{WINDOW_HOURS}h，汛期最大 24h 过程）",
        f"- 窗口总雨量：{sum(vals):.1f} mm；峰值小时：{vals[peak_i]} mm（{times[peak_i].isoformat()}）",
        "",
        "## 实算累计雨量",
        "",
        "| 时长 | 最大累计 | 起点 |",
        "| --- | --- | --- |",
    ]
    for span in (3, 6, 12, 24):
        s, t = stats[span]
        lines.append(f"| {span}h | {s} mm | {t.isoformat()} |")

    lines += ["", "## 响应等级判定（预案：plan_flood_emergency_cd_2026.md 第 11-16 行）", ""]
    for level, desc, span, threshold in RESPONSE_RULES:
        s = stats[span][0]
        mark = "✅" if s >= threshold else "—"
        lines.append(f"- {mark} {level}：{desc} —— 实算 {span}h {s} mm（阈值 {threshold}）")

    if hit:
        lines += ["", f"**判定结果：{hit[0]}**（{hit[1]}；{hit[2]}h 实算 {hit[4]} mm ≥ {hit[3]} mm）"]
    else:
        lines += ["", "**判定结果：未达四级响应启动条件**（6h < 50mm 且无对应预警）"]

    lines += [
        "",
        "## 与原情景的关系（红线）",
        "",
        "- 原情景 `WA-V2-20261101-01`（3h 60mm，橙色）**保持不变**，主链路数据零改动。",
        "- 本变体是并行输入：「如果 2026-08-01 真实暴雨落在 V2 片区」。演示时作为可选输入讲，",
        "  不得声称原推演事件 vd02 由该降雨驱动。",
        "- 全部数字由 `scripts/weather_to_scenario.py` 从 `chengdu_hourly_2026-06-01_2026-09-30.csv` 实算，可复算。",
    ]
    (OUT / "mapping_report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

    print("window total:", f"{sum(vals):.1f}mm")
    for span in (3, 6, 12, 24):
        s, t = stats[span]
        print(f"max {span}h: {s}mm from {t.isoformat()}")
    print("alert:", alert_level, f"(3h={mm3}mm)")
    print("response:", hit[0] if hit else "below level-4")


if __name__ == "__main__":
    main()
