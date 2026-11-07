# -*- coding: utf-8 -*-
"""整备 Open-Meteo 真实气象数据为交付 CSV，并挑出汛期最大暴雨过程。

数据出处（必须随材料一起展示）：
- 来源: Open-Meteo Historical Weather API (https://open-meteo.com/en/docs/historical-weather-api)
- 底层数据: ERA5 / ERA5-Land 再分析（ECMWF）
- 许可: CC BY 4.0，非商用需署名
- 坐标: 30.66N, 104.07E（成都市中心），时区 Asia/Shanghai
"""
import csv
import json
from datetime import datetime
from pathlib import Path

ROOT = Path(r"D:\华为ICT\agent_poc\samples\weather")
RAW = ROOT / "chengdu_2026_flood_season_raw.json"

data = json.loads(RAW.read_text(encoding="utf-8"))
h = data["hourly"]
times = h["time"]
precip = h["precipitation"]
rain = h["rain"]
temp = h["temperature_2m"]
rh = h["relative_humidity_2m"]

# 1) 全量 CSV
csv_path = ROOT / "chengdu_hourly_2026-06-01_2026-09-30.csv"
with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
    w = csv.writer(f)
    w.writerow(["time", "precipitation_mm", "rain_mm", "temperature_2m_c", "relative_humidity_pct"])
    for row in zip(times, precip, rain, temp, rh):
        w.writerow(["" if v is None else v for v in row])
print("csv:", csv_path.name, csv_path.stat().st_size, "bytes")

# 2) 挑最大连续暴雨过程：滑动 24h 累计雨量 top1
def hour_idx(t):
    return datetime.fromisoformat(t)

best_start, best_sum = 0, -1.0
win = 24
cum = [0.0]
for v in precip:
    cum.append(cum[-1] + (v or 0.0))
for i in range(len(times) - win + 1):
    s = cum[i + win] - cum[i]
    if s > best_sum:
        best_sum, best_start = s, i

peak = max(range(best_start, best_start + win), key=lambda i: (precip[i] or 0.0))
print(f"max 24h window: {times[best_start]} ~ {times[best_start+win-1]}  total={best_sum:.1f}mm  peak_hourly={precip[peak]}mm")

# 3) 暴雨过程元数据
meta = {
    "dataset": "chengdu_hourly_2026-06-01_2026-09-30.csv",
    "source": "Open-Meteo Historical Weather API",
    "underlying": "ERA5 / ERA5-Land reanalysis (ECMWF)",
    "license": "CC BY 4.0",
    "location": "Chengdu 30.66N 104.07E",
    "timezone": "Asia/Shanghai",
    "period": [times[0], times[-1]],
    "points": len(times),
    "max_hourly_precipitation_mm": max((v or 0.0) for v in precip),
    "max_24h_window": {
        "start": times[best_start],
        "end": times[best_start + win - 1],
        "total_mm": round(best_sum, 1),
        "peak_hourly_mm": precip[peak],
        "peak_hour": times[peak],
    },
    "usage_note": "真实历史降雨，可整备为推演降雨输入；推演事件 vd02 本身仍为合成情景，不得声称由该降雨驱动。",
}
(ROOT / "chengdu_weather_meta.json").write_text(
    json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8"
)
print("meta written")
