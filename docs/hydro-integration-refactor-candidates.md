# 水动力链路重构候选清单

本清单只记录死代码、重复逻辑和暂缓项；已删除项必须有行为测试，暂缓项不在本轮擅自删除。

| 项 | 处理 | 为什么保留/删除 |
| --- | --- | --- |
| `replay_export.build_replay` 再次调用 `DeductionEngine` | 已删除重复调用 | 回放必须消费同一份 Pipeline 快照，避免规则 id 与报告漂移 |
| `visualize.build_visual_snapshot` 再次调用 `DeductionEngine` | 已删除重复调用 | 可视化与回放共享同一结论，减少第二套编排路径 |
| `quant/hydraulics.py` 与场景工具中的水力公式 | 暂缓合并 | 两处服务不同边界；未完成输入、出处和结果契约对齐前删除会改变场景口径 |
| `scenario_v2.py` 旧的“尚未接入六跳”表述 | 已更新 | P14 已支持可选过程线透传，保留资源 v1 约束说明 |
| `HydrographResult` 原先只放在 `quant/contracts.py` | 已集中到 `hydro_contracts.py` 并由 quant 重导出 | 避免 deduction → quant → optimize 的循环导入，保持既有导入路径兼容 |
| `quant/hydrodynamics.py` 未使用的 `math` 导入 | 已删除 | 静态检查确认无引用，删除不改变过程线计算 |
| MCP 三个工具重复声明 `hydrograph` schema | 已抽取 `_hydrograph_schema()` | 统一 P13 入参说明，避免三处描述漂移；每次返回新对象，保留工具列表隔离性 |
| MCP 三个工具重复解析 `HydrographResult` | 已抽取 `_parse_hydrograph()` | 统一缺省值与 Pydantic 校验入口，不改变错误传播和资源 v1 口径 |
| `visualize.py` 的 `RenderStep` / `RenderAction` | 已删除 | 字段级比对：`RenderStep` 与 `ReplayStep` 七个字段完全相同；`RenderAction` 是 `ReplayAction` 的子集（少 `evidence`）。仓内零调用、零测试引用，`VisualSnapshot.steps` 用 `ReplayStep`、`JOB_SCHEMA` 的 scene/panel 是 `dict`，均不依赖这两个类型；删除后 803 测试（当轮口径；当前口径以实跑为准）与 106/106 自检逐位不变。已知 breaking：外部 `from decision_poc.visualize import RenderStep/RenderAction` 会断，离线 PoC 不承诺该导出稳定 |
| `event_deduce` 与 `collab_run` 的完整推演流程 | 暂缓 | 两者共享部分解析但权限检查与协同编排不同，需单独回归后再合并 |
| `scenario_v2.py` 的“不做水动力计算”模块边界文案 | 暂缓 | 当前仍准确描述该模块职责；P13 由上层过程线链路接入，不应改成模块自身求解 |
| Three Maps 投影/地图循环 | 暂缓 | 与本次水动力链路无直接依赖，需单独基准和边界确认后再动 |
