# 水动力链路重构候选清单

本清单只记录死代码、重复逻辑和暂缓项；已删除项必须有行为测试，暂缓项不在本轮擅自删除。

| 项 | 处理 | 为什么保留/删除 |
| --- | --- | --- |
| `replay_export.build_replay` 再次调用 `DeductionEngine` | 已删除重复调用 | 回放必须消费同一份 Pipeline 快照，避免规则 id 与报告漂移 |
| `visualize.build_visual_snapshot` 再次调用 `DeductionEngine` | 已删除重复调用 | 可视化与回放共享同一结论，减少第二套编排路径 |
| `quant/hydraulics.py` 与场景工具中的水力公式 | 暂缓合并 | 两处服务不同边界；未完成输入、出处和结果契约对齐前删除会改变场景口径 |
| `scenario_v2.py` 旧的“尚未接入六跳”表述 | 已更新 | P14 已支持可选过程线透传，保留资源 v1 约束说明 |
| `HydrographResult` 原先只放在 `quant/contracts.py` | 已集中到 `hydro_contracts.py` 并由 quant 重导出 | 避免 deduction → quant → optimize 的循环导入，保持既有导入路径兼容 |
| Three Maps 投影/地图循环 | 暂缓 | 与本次水动力链路无直接依赖，需单独基准和边界确认后再动 |
