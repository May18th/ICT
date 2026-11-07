# real_20260801 情景覆盖目录（生成物，勿手改）

由 `scripts/build_real_scenario_dataset.py` 生成：samples/assets/urban 全量复制，
仅 weather_alerts.csv 的 active 橙色行换装为真实暴雨映射行。

- 真实数据：Open-Meteo/ERA5（CC BY 4.0），成都 30.66N/104.07E，2026-08-01 过程
  24h 累计 77.7mm、3h 最大 42.1mm、峰值 15.5mm/h
- 演示映射：真实雨量值 + 演示事件时间窗（2026-10-29，与冻结工单台账对齐）
- 预期行为：预警黄色 → 橙色响应规则（需预警 orange/red）不触发 →
  引擎显式输出「无匹配等级 · 需人工复核」，不硬凑等级
- 装载方式：GET /api/demo?scenario=real_20260801（白名单校验，默认口径不变）
- 重算：`python scripts/build_real_scenario_dataset.py`（幂等）

映射方法与真实雨量推导见 `samples/weather/scenario_variant_20260801/mapping_report.md`。
