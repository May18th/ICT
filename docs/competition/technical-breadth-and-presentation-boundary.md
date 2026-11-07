# 技术广度呈现口径 + 呈现层建模边界

- 日期：2026-10-31
- 目的：回答两件事——①单个内涝场景里怎么把"技术广度"讲成可验证的纵深；
  ②`blender-ue-scene` 的 3D 建模是否偏题、偏在哪、怎么收回。
- 判定依据：`docs/competition-gap-audit.md`（五考点）、`docs/p10-visualization-tools-design.md`（呈现层契约）、
  `blender-ue-scene/scenes/WO-20261029-007/log.md`（S1–S28 实测记录）。

## 0 结论

1. **广度不是场景数量，是"考点覆盖 × 每层证据强度 × 层间耦合度"**。赛题五考点是技术能力维度，
   评分表里没有"支持多少种城市/事件"这一列；扩场景只会增加未验证面。
2. **语义/本体/资产建模没有偏题**，它是考点②④本身，也是整条链的唯一真源。
3. **偏题的是呈现层的 3D 视觉生产**（场景日志 S23–S28）。它不在任何考点里，且有自己独立的
   "完成"标准（面数/贴图/机位/帧率），会持续吸走时间；更关键的是它自建了一套 twin_id，
   与决策层真源零交集，导致"六跳驱动高亮"在技术上无法实现。
4. **呈现层本身是加分项，但必须先挂回 P10 契约**（`scene_render` → RenderJob → `apply_job` → ack），
   否则就是两套并行呈现层。

## 1 广度的呈现口径：同一事件 ID 穿过 8 层，每层一个可核对数字

一个事件（`WO-20261029-007`）贯穿全部层，不是三个并列 demo。

| # | 层 | 技术点 | 本事件锚点 | 可核对证据 | 状态 |
| --- | --- | --- | --- | --- | --- |
| 1 | 多模态资产 | 内容寻址 + locator + 版本/有效期/可见性 | 19 资产（表格 10 / 文档 8 / 示意图 1） | 解析 19/19、未识别列 0 | 已核实 |
| 2 | 本体半自动构建 | 种子本体→规则抽取→置信度分档→人工确认→发布/回滚 | 123 三元组 / 8 谓词 | 改 1 格只产 2 条候选；回滚哈希逐位一致 | 已核实 |
| 3 | 时空语义层 | `as_of` 查询 + `snapshot_hash` 锚定 + 权限前置 | 63 个 twin（`TW-*`） | 检索命中 100% | 已核实 |
| 4 | 跨部门推演 | 六跳固定 + 规则数据化（带原文行号）+ 三类冲突 | 六跳 / C1 权限交叉 / C2 职责空白 / C3 资源争用 | 六跳完整率 100%、冲突三类全保留、缺口 2 台 | 已核实 |
| 5 | 反事实 | 同一 DAG 干预后重放 | CF1 封路 / CF2 库存 6→2 / CF3 预警升级 | 820 m/1.2 min → 3860 m/6.3 min；缺口 2→6 | 已核实 |
| 6 | 溯源与血缘 | claim 引用闸门 + append-only 哈希链 + 三层反向索引 + 变更传播 | asset→triple→claim→decision | 引用完整率 100%、漏报 0、重算范围 <30%、篡改可定位 | 已核实 |
| 7 | 决策进化 | 偏差归因→候选→评测门禁→采纳/拒绝→版本标签/回滚 + 分层记忆 | 抽排分配策略 v1.0→v1.2 | 训练 0.2264→0、留出 0.2182→0、3 轮 2 采纳 1 拒绝 | 已核实 |
| 8 | 编排与平台 | 15 MCP 工具（决策 10 + 呈现 3 + 引导 1 + 协同 1）+ 5 Skill 编排 + 2 SKILL.md + Nexent Agent | `urban_deduction_agent` v19（DeepSeek-Chat） | 真机 8/8 一致、conversation_id=120、1 次工具调用日志 | 已核实 |
| 9 | 呈现层（加分项，非考点） | `scene_render`/`scene_compare` → RenderJob → UE `apply_job` → ack | 六跳高亮 + 分配面板 | 工具调用日志 `artifacts/visual/tool_log.jsonl` + ack | **主键已闭环（P10.1），UE 现场演出待验** |

答辩口径一句话：**"同一个事件 ID，从资产到决策到回滚共 8 层，每层有一个可当场跑出来核对的数字。"**
不要说"我们支持多种城市/多种事件"。

## 2 不扩场景也能造出"广度"的四种合法方式（均已具备）

| 方式 | 载体 | 为什么算广度 |
| --- | --- | --- |
| 用例类型矩阵 | 22 例：正常 7 / 冲突 3 / 证据不足 6 / 过期 4 / 越权 2 | 覆盖的是**边界条件**，比多场景更能证明鲁棒性 |
| 时态 | 2021 已废止预案 vs 2025 修订预案共存 | 同一问题在不同 `as_of` 下结论不同，证明时态语义层不是摆设 |
| 策略版本 | `flood-response-v1.0 → v1.1 → v1.2`，可回滚 | 证明"可进化"不是一次性调参 |
| 反事实 | CF1 / CF2 / CF3 | 证明推演是因果模型，不是查表 |

## 3 建模的三层分界与判定规则

| 类型 | 产物 | 是否服务考点 | 判定 |
| --- | --- | --- | --- |
| 语义/本体/资产建模 | `samples/assets/urban/scene.json`（63 twins）、`scene_layout.json`、rules 原文行号、claim 定位符 | 是考点②④的地基 | **不偏题**，且必须是唯一真源 |
| 引擎无关场景数据 | `visualize.py` / `scene_export` / RenderJob | 是考点③工具层 | **不偏题** |
| 3D 视觉生产 | Blender/UE 的植被、雨效、立面变体、渲染调优 | 不在五考点内 | **偏题**（除非按第 4 条验收） |

判定规则（一句话）：**任何一次 3D 投入，必须能回答"它让哪一条推演结论在画面上被验证或被证伪"。
答不上来就是视觉生产。**

## 4 当前偏题的具体证据

1. **真源分叉（2026-11-01 已修）**：3D 侧 24 个实体的 twin_id 是 `RD-DONGHUAMEN` / `TN-TIANFU` / `FAC-PS-01` /
   `RES-PUMP-TEAM` / `DEPT-EMERG`（`scenes/WO-20261029-007/semantic_entities.json`），
   决策层 63 个 twin 是 `TW-road-R-101` / `TW-pump_station-DF-PS-01` / `TW-resource-RES-PUMP-M`
   （`samples/assets/urban/scene.json`）。**两套 ID 零交集，仓库里没有映射表。**
   现状：P10.1 已把实体表重新投影成 `TW-*`（`samples/assets/urban/semantic_entities.json`），
   旧命名空间整体退役、不做伪映射，见 [实体重投影与绑定表](../scene-binding.md)。
2. **两套呈现层**：P10 已定义 `scene_render → RenderJob → urban_twin_state.apply_job() → ack` 契约，
   UE 关卡 `Maps/UrbanTwinDemo` 与 4 套场景（normal/flood/closure/construction）都从 `scene_layout.json` 推导；
   而 S21–S28 另建 `Maps/UrbanTwinLevel`，`build_urban_level.py` 产出的 `TW-` 标记物被当作
   "旧迭代遗留、未确认用途"保留而未使用（S25 记录）。
3. **S19 定的呈现层验收（2026-11-01 23:16 已闭环）**：S22 曾用"选中 + 聚焦 + 重命名"替代材质高亮，
   随后 S23–S28 转入植被/雨效/立面/城市重建，`steps[i].highlights → Actor tag` 的高亮切换一直没做。
   现已实现并实测通过：`Content/Python/urban_twin_state.py` 读 `artifacts/visual/jobs/` 最新作业，
   逐跳按 `highlights` 的 twin_id 匹配 Actor 的 `twin:` tag 换 `M_Highlight`。
   实测数据：关卡内 **23 个** twin 标签，作业 **14 跳**、计划高亮 **100 次**、可命中 **91 次**；
   缺的 9 次全部来自 3 个已披露"无几何"的对象（见 §5.1 与 `../scene-binding.md` L78–80），不是新缺口。
   证据：`artifacts/visual/acks/snap-3d7714d7230412bf.json`（含逐跳 `planned`/`hit`/`missing`）。
   **仍缺一项**：`-nullrhi` 不渲染，数字对不等于"屏幕上变色了"，需补高亮前/后对照图。
4. **投入产出倒挂**：S23 单张 1080p 渲染 50.9 s → 170 s；草地 9.4 万实例把 8 GB 显存顶满后强制结束
   Blender；15.3 GB 内存下 UE 与 Blender 不能同时开。这些成本不产生任何评分点。

## 5 冻结与最小闭环（估算 0.5–1 人日）

冻结：S29 起停止一切视觉 pass（植被/雨效/立面/材质变体）。S23–S28 已产出画面仅作 PPT 背景/录屏素材，
不作废、也不再迭代。

最小闭环三项：

1. **实体重投影（2026-11-01 已完成）**：从 `scene.json` 的 63 个 `TW-*` 中筛出本事件相关子集，
   `hop` 直接取推演回放的 `steps[i].stage`，重新生成实体表；旧 24 个 OSM 实体整体退役，
   底图降级为背景。产物 `samples/assets/urban/semantic_entities.json`（指纹
   `proj-c19ea6550ab6f802`），23 个实体全部可画、3 个高亮对象无几何显式列出。
   规则与验收见 [实体重投影与绑定表](../scene-binding.md)。
2. **高亮驱动（2026-11-01 已闭环）**：UE 侧读 `artifacts/visual/jobs/` 最新作业 → 按 `steps[i].highlights`
   的 twin_id 查 `twin:` tag → 换 `M_Highlight` + 面板文本；复用 `urban_twin_state.apply_job()`，未新写通路。
   本轮只补了"可核对"三件：`preflight()` 逐跳核对、`highlight()` 记空缺 id、`ack_local()` 写
   `planned_highlights`/`resolved_highlights`/`missing_twin_ids`。
   **修复前的问题**：命中 0 个对象也会照写"已演出"，这是验收一直收不了口的直接原因。
   待办：用带 RHI 的编辑器补一张"高亮前/后"对照图，把 ack 数字和画面接上。
3. **两条负例**（比任何画面都能证明可信溯源）：
   - 证据不足（I01 拒答）时**不**高亮任何对象；
   - 账本被篡改时 `verify` 定位断点，画面切到"结论不可信"状态而不是继续演。

验收：一条命令落盘 RenderJob → UE 自动演出并写 ack → `scene_render` 返回 `acked` →
`artifacts/visual/tool_log.jsonl` 有对应记录。做到这四条，呈现层才算挂回考点③。

四条逐项状态（2026-11-01）：

| 条 | 状态 | 证据 |
| --- | --- | --- |
| 落盘 RenderJob | ✅ | `artifacts/visual/jobs/snap-3d7714d7230412bf.json`（14 跳） |
| UE 自动演出并写 ack | ✅ | `artifacts/visual/acks/snap-3d7714d7230412bf.json`（91/100 命中，含逐跳核对） |
| `scene_render` 返回 `acked` | ✅（事后查询） | 调用当时按异步设计返回 `delivery=pending`（`tool_log.jsonl` 第 6 条），ack 落盘后 `delivery_state()=acked` 实测 2026-11-01 23:18 |
| 画面证据 | 🟡 部分 | 自动出图通道已打通（`StartupScripts` 钩子），三张图非空；但三张 MD5 相同，高亮画面未体现，原因未定，详见 `blender-ue-scene/docs/ue-接入待办.md` §0.9 |

## 6 时间回流（按 `competition-gap-audit.md` 优先级）

| 优先级 | 事项 | 现状 |
| --- | --- | --- |
| P0 | 官方模板 1/模板 2 与报名通知原文落库 | 阻塞在用户侧 |
| P1 | 模板 1 作品信息 Word 套版 | 内容草稿已备，未套版 |
| P1 | Demo 地址（公网/云上） | 仅 localhost:8787 |
| P1 | SKILL.md 上传平台技能仓库 | ✅ 已完成（2026-11-02）：skill_id=2/3，正文逐位一致，证据 `samples/nexent/live/skills.json` + `captures/nexent/skill-space.png`；**已绑定到已发布 Agent**（草稿 v0 与发布版本 v19 各 2 条实例、全部 enabled，2026-11-06 复核，证据 `samples/nexent/live/skill_bindings.json`） |
| P2 | 真实用户评分 3–5 人（进化前后各一次） | 待组织 |
| P2 | 平台界面截图 / 演示录屏 | 截图 ✅（3 张 + `platform_ui.json`，sha256 进自检）；问答过程录屏仍待补 |
| P2 | 应用价值量化（决策耗时、CF1 通行代价、缺口上报提前量） | 待做 |

## 7 对外数字一致性（2026-10-31 已修，历史记录）

> 本节是 2026-10-31 那次修复的历史记录，里面的 **516 / 498 / 483** 是当时口径，已过时。
> **当前口径（2026-11-07）：913 项测试（收集数）、对外数字自检 108/108、期望指纹 `14070e22fff848e6`**，
> 以 `pytest -q` 与 `python -m decision_poc.evidence_check` 的实跑输出为准。

实测 `python -m pytest -q`：**913 项测试**（2026-11-07 复核实测，含微调度台取数层 17 项、跨材料数字守卫 4 项、页面结构 1 项新增用例）。
原先 `README.md` / `competition-gap-audit.md` 写 498 项、`preliminary-submission-draft.md` 写 483 项，
三处已统一为 **516**；期望指纹也重新生成（旧文档里 `a93666bf` / `f6f67337` 两个指纹本身已经互相不一致，一并修掉）。

冻结方式（两个锚点，缺一处即失败）：

- `samples/eval/evidence_expectations.json` → `test_baseline.collected = 516`：
  `tests/test_test_count_guard.py` 用 pytest 会话实测值 `request.session.testscollected` 核对；
  加/删测试必须同步改期望，否则测试直接失败（改测试数量要说明原因）。
- 同一期望文件 → `checks.test_suite.docs_declared = [516]`：
  `python -m decision_poc.evidence_check` 扫描三份对外文档，任何一份写回旧数字即报漂移（退出码 1）。
  当前自检口径为 **108/108 项一致**（期望指纹 `14070e22fff848e6`）。

## 8 2026-10-31 复核结果（结论已用实测数据核对）

### 8.1 与第 4 节一致的证据

| 断言 | 实测 |
| --- | --- |
| 决策层 63 个 twin | `samples/assets/urban/scene.json.objects` = 63，15 种 kind |
| 3D 侧 24 个实体与决策层零交集 | `semantic_entities.json` 24 个（`RD-`/`TN-`/`AREA-`/`FAC-`/`RES-`/`DEPT-`），交集 = 0 |
| 两套呈现层 | `build_urban_level.py` 写的才是契约 tag（`twin:TW-road-*` 等）；S21–S28 产出的是 `Maps/UrbanTwinLevel` |
| 高亮未闭环 | `artifacts/visual/acks/` 为空；最新作业 `snap-3d7714d7230412bf.json` 的 `status=pending` |
| 测试数不一致 | 实测 516；README/gap-audit 写 498、draft 写 483（已修，见 §7） |

### 8.2 复核中新发现（会影响闭环，按优先级）

1. **作业结构不是顶格 `steps`**：RenderJob 的六跳挂在 `scene.steps[]`（本次 14 跳）+ `scene.actions[]`，
   `urban_twin_state` 的说明写"`steps[]` 六跳"，UE 侧取数要按 `scene.steps`，否则读到空数组。
2. **19 个高亮里 1 个不可解析**：`TW-water-WO-20261029-007` 是 `replay_export.py:122` 用
   `f"TW-water-{event_id}"` 拼出来的，`scene.json` 里没有这个 twin（18/19 命中）。
   `observed` 跳会去亮一个不存在的对象——闭环前必须先决定：要么映射到 scene 里已有的水面 twin，
   要么把它登记进真源（后者会改 `snapshot_hash`，需一次有说明的期望更新）。
3. **文档里的期望指纹本身也不一致**（README `a93666bf` vs runbook `f6f67337`），说明"数字冻结"此前
   没有覆盖文档层；本轮把测试数与指纹一起纳入机器核对与再生成。

### 8.3 结论

第 0 节四条判定全部成立；"呈现层偏题"按第 3 节判定规则成立。修复路径就是 §5 的三项最小闭环
（0.5–1 人日）：先做实体重投影 + 高亮驱动，再补两条负例；每完成一项，用 §5 的四条验收回挂考点③。

## 9 收回执行记录（2026-10-31，A 方案：映射到真源已有 twin）

按 §5 最小闭环执行，详细报告见 [presentation-closure-report.md](presentation-closure-report.md)。

### 9.1 实体重投影（已完成）
- `scene_layout.py` 四类对象新增 `twin_id`（真源规则：`TW-road-*` / `TW-<kind>-<pipeline_id>` /
  `pump_station` / `intake→TW-drainage-*` / 水面→`TW-incident-*`）；
- `replay_export.py` 删除 `f"TW-water-{event_id}"` 假 id，改为 `resolve_flood_twin()`：
  优先 incident twin、其次真源 water twin、找不到返回空串；
- `build_urban_level.py` 改为消费布局 `twin_id`；UE 关卡 `UrbanTwinDemo` 回填 5 个 Actor tag 并保存；
- 新增 `tests/test_presentation_contract.py` 5 条不变量。
- 实测：布局 23 个 twin_id 全部在 `scene.json` 内；RenderJob 19/19 高亮 id 命中真源（改前 18/19）；
  第一跳高亮 = `TW-incident-WO-20261029-007`。

### 9.2 高亮驱动与验收
- RenderJob 落盘（14 跳 / 19 高亮，`snap-3d7714d7230412bf`）；
- UE 侧按第一跳 id 换 `M_Highlight`（水面变青色，已人工确认一次）；
- `POST /api/scene/<id>/ack` → `{"status":"acked"}`，ack 文件落盘，`delivery_state` 返回 `acked`；
- `artifacts/visual/tool_log.jsonl` 有 `origin=mcp` 的 `scene_render` 记录。

### 9.3 两条负例（数据层已成立）
- I01 未知事件：`event_deduce` → `NOT_FOUND`；`scene_render` → `EVIDENCE_INSUFFICIENT`；
  `jobs/` 无新增作业 → 不会高亮；
- 账本篡改：篡改前 `verify ok`；篡改后 `first_broken=PV-0001-13ca217b`，`HASH_MISMATCH` → 可定位断点。

### 9.4 UE 内侧画面证据（已补拍）
- 同一 MCP 会话内完成 load_level → 按 `twin:` tag 找 Actor → 换 `M_Highlight` → 截图，
  证据入 `docs/reports/captures/`：基线、正例（第一跳高亮）、I01 不高亮、账本篡改"结论不可信"四张；
- 关卡 `UrbanTwinDemo` 已保存为干净状态（横幅删除、材质 override 重置）；
- 注意：跨 MCP 会话时 scene 工具可能仍绑定旧 world，重拍务必在同一会话内完成；
  TextRender 默认字体缺中文字形，横幅用 ASCII/或换 Font 资产。

### 9.5 回归
- `pytest -q` → 521 passed（测试数冻结在 `test_baseline.collected=521`）；
- `evidence_check` → 108/108 项一致，期望指纹 `14070e22fff848e6`。
