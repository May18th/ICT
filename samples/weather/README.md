# 成都真实气象数据（汛期逐小时）

## 这是什么

成都市中心（30.66N, 104.07E）**2026-06-01 ~ 2026-09-30 逐小时观测/再分析数据**，作为推演链路的真实降雨数据来源候选。

## 文件

| 文件 | 内容 |
| --- | --- |
| `chengdu_hourly_2026-06-01_2026-09-30.csv` | 2928 行逐小时：precipitation_mm / rain_mm / temperature_2m_c / relative_humidity_pct |
| `chengdu_2026_flood_season_raw.json` | API 原始返回（留档备查） |
| `chengdu_weather_meta.json` | 出处、许可、最大暴雨过程等元数据 |
| `../scripts/prepare_weather.py` | 从 raw.json 重建上述全部产物（可复算） |

## 出处与许可（引用时必须带）

- API：Open-Meteo Historical Weather API
- 底层数据：ERA5 / ERA5-Land 再分析（ECMWF）
- 许可：**CC BY 4.0**（使用需署名）
- 时区：Asia/Shanghai

## 汛期最大暴雨过程（真实）

- **2026-08-01 01:00 ~ 08-02 00:00（24h 累计 77.7 mm，峰值小时雨强 15.5 mm/h）**
- 按国标 GB/T 28592，24h ≥ 50 mm 即「暴雨」，本过程达标。

## 使用边界（红线）

- 这是**真实历史数据**，可以标注为真实来源；
- 推演事件 vd02 是**合成情景**，**不得声称由该降雨驱动**；
- 若后续把该序列接入推演输入（`weather_to_scenario` 映射），须在材料里写明映射规则并留脚本，保持可复算。
