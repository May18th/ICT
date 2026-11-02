# P10 可视化工具层设计：把 Blender/UE 变成智能体可调用的工具

## 问题

P5 起的 MCP 服务有 9 个工具，全部是决策语义工具（语义查询、证据定位、六跳推演、
反事实、分配）。**没有任何工具能让智能体触发一次渲染。** 可视化通路是：

```
replay_export.py → samples/replay/core_run.json → 人工在 UE 编辑器里敲 s.show_step(3)
```

两处偏差：

1. 智能体手上没有可视化工具，无法触发——演示时"场景变了"与"智能体决策了"之间
   没有可核对的动作；
2. 唯一的触发方式是人手敲命令，正是"三维演示"而不是"决策动作"。

结论：缺口不是 UE 不够实时，是**触发权**和**工具在册**。本阶段只补这两条，不引入
实时闭环。

## 定位

| 层 | 承担者 | 职责 | 频率 |
| --- | --- | --- | --- |
| 决策层 | LangGraph / Nexent | 六跳推演、P9 策略进化 | 每轮 |
| 工具层 | `urban-twin-mcp` | 现有 9 个 + 本阶段 3 个 | 按需 |
| 呈现层 | UE / Web | 演快照、并排对比 | 决策定稿后 |
| 资产层 | Blender | 离线生成 4 套场景资产 | 初始化 / 低频 |

## 三条硬约束

1. **只接受快照 id，不接受低层渲染参数。** 不开放位姿、材质、镜头。开放低层参数
   会让决策循环被渲染细节污染，演示不可复现。
2. **异步、可降级。** 渲染作业落盘即返回，不等引擎渲染完成。引擎没起时返回
   `UPSTREAM_UNAVAILABLE`，决策层照常出结论。渲染不得进主循环的强依赖路径。
3. **同一决策同一 id。** `snapshot_id = sha256(event_id, as_of, 策略, snapshot_hash)[:16]`，
   重跑逐位一致。"渲染的是不是刚才那份结论"可以当场核对。

## 数据结构

```
VisualSnapshot   一份可寻址的决策快照（六跳步骤 + 动作 + 分配方案 + 哈希）
      │  scene_render
      ▼
RenderJob        engine-neutral 演出单（场景状态 / 每步高亮 / 面板文本）
      │  HTTP GET /api/scene/<snapshot_id>
      ▼
UE urban_twin_state.py   apply_job() → apply_scenario + show_step + 面板
```

`VisualSnapshot` 只是把已经在算的东西组装起来，不新增推理：

| 字段 | 来源 |
| --- | --- |
| `steps` / `actions` | `replay_export.build_replay_from_report`（P6 六跳） |
| `allocation` | `allocation.decide(decision_inputs, policy)`（P9） |
| `snapshot_hash` | `SkillPipeline` 知识快照 |
| `scene_hash` | `scene_export.build_scene_payload()` 的规范化哈希 |
| `scenario` | 事件深度 + 受影响对象的 `lifecycle_state` |

## 三个工具

### `scene_render`（presentation 组）

输入：`snapshot_id` 或 (`event_id`, `as_of`, `policy`)。二选一，都给 `snapshot_id` 优先。

输出：`snapshot_id` / `job_id` / `scene_hash` / `scenario` / `job_path` /
`step_count` / `highlight_count` / `allocation_total` / `delivery`。

`delivery` 只有两档，不谎报：

| 值 | 含义 |
| --- | --- |
| `pending` | 作业已落盘，引擎尚未认领 |
| `acked` | 引擎侧已写回 `acks/<snapshot_id>.json` |

> 不返回"渲染完成"：本工具不持有渲染器连接，谎报就变成假证明。

### `scene_compare`（presentation 组）

输入：`baseline_id`、`candidate_id`。

输出：策略差异（改了哪几个参数）、分配差异（按片区 delta、调拨变化、未满足变化）、
指标差异、高亮对象差异，以及一句可直接讲给评委的 `verdict`。

这是 P9"从哪一次推演、调了什么策略、下一次偏差缩小多少"的可视化出口：
v1（全零权重、不跨区）与进化后版本在同一事件上跑，分配向量不同即得差异。

### `asset_lookup`（bootstrap 组）

输入：`scenario`（normal/flood/closure/construction）。

输出：`glb`/`blend` 路径、`exists`、`stale`、对象计数、离线生成命令。

> **为什么不叫 `asset_ensure`**：Blender 生成是离线动作（分钟级，且要开 DCC）。
> 把它塞进推演循环会让一轮决策从秒级变成分钟级。所以本工具只做"核对与指路"，
> 缺失时返回可执行的离线命令，不启动 Blender。

## 工具分组（allowlist）

`tools/list` 支持按组过滤，默认全量（向后兼容）：

| 组 | 工具 | 启用阶段 |
| --- | --- | --- |
| `decision` | 现有 9 个 | 推演 / 进化 |
| `presentation` | `scene_render`、`scene_compare` | 决策定稿后的呈现 |
| `bootstrap` | `asset_lookup` | 初始化 |

目的：智能体在推演循环里看不见渲染工具，就不会把推理预算花在"要不要换个镜头"上。

## 演示时序（谁触发）

| # | 动作 | 执行者 |
| --- | --- | --- |
| 1 | `scene_render(v1)` | 智能体 |
| 2 | 注入扰动 | 人 |
| 3 | `allocation_plan` 重算 → `scene_render(evolved)` | 智能体 |
| 4 | `scene_compare(baseline, candidate)` | 智能体 |
| 5 | 评委锁定某部门资源 → 回灌为约束 → 下一轮 | 人 → 智能体 |

关键证据是**工具调用日志**：UE 场景旁同步滚动的 MCP 调用记录，是"智能体触发"
唯一的可核对证明。日志落在 `artifacts/visual/tool_log.jsonl`，Web 面板与 MCP 服务
共用同一路径与格式。

## 验收

- `snapshot_id` 对同一输入稳定、对策略变化敏感。
- `scene_render` 落盘作业可被 `/api/scene/<id>` 取回，字段与 UE `apply_job` 对齐。
- `scene_compare` 在 v1 vs 进化版本上给出非空分配差异。
- 引擎未起时 `scene_render` 返回 `pending` 而不是报错。
- MCP 全量工具数为 12；分组过滤生效。
- UE 侧不再需要手动敲 `show_step` 即可完成一次演示。

## 不做什么

- 不做 UE 像素流（浏览器可交互已由 `web_demo.py` 承担）。
- 不做实时逐帧同步（异步快照回放足够撑起"决策 → 渲染 → 对比"叙事）。
- 不在推演循环里启动 Blender / UE 进程。
