# Twin3D 深度事件镜像实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 为 `/twin3d` 增加六跳时间轴、可点击对象详情、空间高亮和证据面板。

**Architecture:** 保持 `build_twin3d_html()` 为现有入口，新增统一的前端状态对象，将 scheduler 数据映射为时间轴与对象索引；Three.js 只负责空间渲染，DOM 面板负责解释信息。所有状态由同一跳次状态驱动。

**Tech Stack:** Python 3.12、内嵌 HTML/JavaScript、Three.js、现有 `/api/scheduler` 数据。

**Spec:** `docs/superpowers/specs/2026-11-08-twin3d-deep-event-mirror-design.md`

## Global Constraints
- 本轮不新增外部依赖和生产系统接入；生产接入属于最终拓展方向。
- 不改变主页面既有业务逻辑和 API 契约。
- 所有模拟状态必须明确标记。

## Review Focus
- scheduler 缺少可选字段时页面仍可渲染。
- 六跳数据为空或顺序异常时控件不崩溃。
- 点击对象后切换跳次，详情与空间高亮保持一致。
- 共享材质不能造成不同对象状态串扰。
- 页面初始化未完成时，时间轴控件不能抛出错误。

### Task 1: 数据状态模型与时间轴
**Files:** Modify `src/decision_poc/web_demo.py`; Test existing route/render checks.
- [ ] 从 scheduler 响应建立统一 `twinState`，包含 jumps、currentJump、selectedEntity。
- [ ] 将六跳控件接入播放、暂停、上一跳、下一跳、重播。
- [ ] 为空数据和缺失字段提供可读降级文本。
- [ ] 运行定向测试与编译检查。

### Task 2: 可点击对象与详情卡
**Files:** Modify `src/decision_poc/web_demo.py`.
- [ ] 为积水、泵站、道路和封路对象建立对象索引与可点击命中区域。
- [ ] 增加详情卡，展示 ID、名称、状态、道路、责任部门和关联跳次。
- [ ] 支持从详情卡跳转到关联跳次。
- [ ] 验证对象切换和空对象状态。

### Task 3: 空间高亮与证据面板
**Files:** Modify `src/decision_poc/web_demo.py`.
- [ ] 将当前跳次映射为积水、泵站、道路和护栏的视觉状态。
- [ ] 为对象使用独立材质或可恢复的基线状态，避免共享材质串扰。
- [ ] 增加证据面板，展示输入、判断、动作、结果及模拟/确认标识。
- [ ] 验证播放、手动跳转、对象选择三者同步。

### Task 4: 主页面联动与回归验证
**Files:** Modify `src/decision_poc/web_demo.py`; possibly update `docs/`.
- [ ] 增加回到主页面对应事件位置的链接或按钮。
- [ ] 运行 pytest、锁文件检查、compileall、pip check。
- [ ] 用浏览器或可用 headless 方式验证六跳和对象详情关键状态。
- [ ] 检查 git 状态，仅提交本次明确修改路径。

