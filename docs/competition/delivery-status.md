# 交付状态核对表（已完成 / 部分完成 / 设计中 / 未开始）

- 版本：`delivery-v1`，核对日期 2026-11-02
- 目的：回答一个问题——**现在直接拿去报名/答辩，会被卡在哪**。状态必须有命令或文件证据，不靠印象。
- 配套：[答辩主线](answer-mainline.md)、[业务价值证据](value-evidence.md)、[差距审计](../competition-gap-audit.md)。

## 0. 状态口径（不允许混用）

| 状态 | 定义 | 例子 |
| --- | --- | --- |
| **已完成** | 有产物 + 有命令/测试能当场验证 | 复现脚本、真机 8/8、两条负例 |
| **部分完成** | 主链路通了，但缺验收条件（单环境、缺截图、缺绑定等） | 可视化闭环（只本机）、真机回归（单事件） |
| **设计中** | 有设计/有代码，但**没有进入主链路或没有验收证据** | 多智能体协同、定量层 |
| **未开始** | 只有想法或只有别人家的模板 | PPT、录屏、真实用户评分 |

## 1. 交付物矩阵

| 交付物 | 官方要求 | 现状 | 状态 | 证据 / 路径 | 阻塞方 | 下一步验收标准 |
| --- | --- | --- | --- | --- | --- | --- |
| 模板 1 作品信息收集表（Word） | 初赛唯一提交物，需按官方版式 | 内容稿已成稿并可重生成（六页：五考点/数据集/关键代码/指标/截图/边界），**官方版式未套** | 部分完成 | `docs/competition/初赛作品信息收集表-内容稿-v0.1.docx`；`scripts/build_preliminary_docx.py` | 用户侧（官方模板原文未入仓） | 套官方模板后导出 PDF，逐项对照官方要求打勾 |
| 项目 PPT（初赛） | 覆盖背景/价值/创新/数据/代码/指标 | **无任何 `.pptx`** | 未开始 | 全仓 `*.pptx` 检索为空 | 我方 | 10 页内，含主线三卖点 + 一屏业务价值 + 负例截图 |
| 模板 2 答辩 PPT | 决赛 | 无 | 未开始 | — | 我方 | 进决赛后再做，初赛不投入 |
| 公网 Demo 地址 | 可访问演示地址 | 已发布：`https://urban-twin-demo.app.workbuddy.host/?sharecode=K4MXY6WYTS`（发布包 `agent_poc/deploy-webdemo/`，**是裁剪子集不是逐字节镜像**：`src` 60/64 个模块，只留 Web Demo 依赖；`samples` 只含演示用证据，其中 `agents.json` 仍是 openPangu、`llm_decision_platform.json` 已于 2026-11-06 用仓库版覆盖但**尚未重新发布**） | 部分完成（证据待重发） | 2026-11-04 复验：`/`、`/evolution`、`/api/demo`、`/api/llm-decision`、`/api/platform-evidence` 全 200；`/api/llm-decision` 公网真调 DeepSeek 选中 `opt-3`=确定性参照（t=0、约 4.1s）。**`/api/platform-evidence` 目前仍返回 11-04 快照**（`selected_option_id=opt-1`、`selected_match=false`），重发前不能对评委说"页面显示命中 opt-3" | 用户侧（WorkBuddy 发布通道，我方无权限） | 重新发布后，`/api/platform-evidence` 返回 `opt-3`/`true`；再用**完整含 `?sharecode=`** 的链接在真实浏览器打开并录屏（卡片内嵌预览不带 sharecode，会显示 Not Found，属已知限制） |
| 完整代码 + 数据集 | 可复现、开源 | 已推 `origin/main`；`LICENSE`（MIT）、`scripts/reproduce.py` 均在仓 | 已完成 | `git log -1`；`LICENSE`；`scripts/reproduce.py` | — | 干净 clone 后按 README 跑 `reproduce.py` 全绿 |
| 开源截图 | 决赛材料 | 仓库已公开、`LICENSE` 已入库；平台界面截图已有 4 张 | 部分完成 | `docs/reports/captures/`；`platform_ui.json`（sha256 进自检） | 我方 | 界面截图 + 开源协议页截图一起入 PPT |
| README / 部署说明 | 一键复现 | README 有本地快速开始；**Nexent 部署说明仍是本机 docker 手工步骤** | 部分完成 | `README.md`、`docs/demo-runbook.md` | 我方 | 一页 `deploy/nexent/README.md` 能从零起平台并跑 8/8 |
| 一键复现脚本 | 从干净环境复现 | `scripts/reproduce.py` 覆盖测试/数据集/推演/反事实/进化/自检 | 已完成 | `python scripts/reproduce.py` | — | 跑完输出全绿；报告里不写死时间 |
| 演示录屏（问答过程） | 常见要求 | **没有**。平台截图脚本只录页面浏览，不录"提问→调工具→出六跳" | 未开始 | `deploy/nexent/capture_platform_ui.py` 只截页面 | 我方（需 10 分钟录屏） | 一段 3–5 分钟：提问 → 工具调用 → 六跳 → 证据 → 面板 |
| SKILL 绑定已发布 Agent | 考点③ | 2 个 SKILL.md 已上传平台技能仓库（skill_id=2/3）；**未绑定到 Agent**（平台要求草稿态建实例） | 部分完成 | `samples/nexent/live/skills.json`、`captures/nexent/skill-space.png` | 我方 + 平台限制 | 平台 Agent 详情里看到技能实例引用 |
| Nexent 多事件真机回归 | 考点⑤可信度 | 已跑通 1 个事件（8/8、`conversation_id`、工具调用日志）；**未做多事件** | 部分完成 | `docs/reports/nexent-live-integration-report.md`；`samples/nexent/live/` | 我方（平台在线即可做） | 至少 3 个事件，结论与离线逐条比对，记录 id |
| 真实用户评分（3–5 人） | 反馈来源合法性 | 采集通道就绪（`/evolution` 表单 + `review_collect`）；**样本为 0** | 未开始 | `docs/real-feedback-channel.md` | 用户侧（组织 3–5 名真实用户） | `reviews/` 有 ≥3 人、进化前后各一次 + 理由 |
| 负例正式演示 | 反"瞎编" | 两条负例数据层 + 四张画面证据已入仓；**未进正式演示脚本** | 部分完成 | `docs/reports/captures/S31-ue-negative-*.png` | 我方 | 演示脚本第 5 步固定插入，30 秒讲完 |
| 业务价值量化 | 应用价值 | 40 分钟 / 3040 m / 0.2264→0 等可复算；**"人工协同耗时"未实测** | 部分完成 | [业务价值证据](value-evidence.md) §2–§4 | 我方（15 分钟实测） | 3 人测一次，回填对照表 |
| 多智能体协同进主链路 | 创新性 | 内核 + 32 测试已落地；MCP 工具、Nexent 未接 | **设计中** | `src/decision_poc/collab/`；`docs/reports/p12-collab-layer-report.md` | 我方 | `collab_run` 注册为 MCP 工具并在平台真机调用一次 |
| 定量层进主链路 | 专业性 | 水文/水力/简化水动力/不确定性内核 + 93 测试 + 手算对拍；**六跳缺口仍走 v1"80% 折算"** | **设计中** | `src/decision_poc/quant/`；`docs/reports/p12-quant-layer-report.md`、`docs/reports/p13-simplified-hydrodynamics-report.md` | 我方（需先补台账字段，见下） | 六跳跳 5 的缺口由 `Q=ψqF` + `能力=额定−故障` 算出，且对外数字一次改全 |

## 2. 本轮核对发现的"口径已过期"（必须改，否则自检会红）

| 位置 | 过期说法 | 实际 |
| --- | --- | --- |
| `docs/competition-gap-audit.md` §4 开源合规 | "无 `LICENSE`" | `LICENSE`（MIT）已在仓 |
| 同上 §4 Git | 写死 `6f7c559` | 以 `git log -1 --oneline` 为准（每轮自动推送） |
| 同上 §4 测试数 | 写死旧测试数 | 以 `python -m decision_poc.evidence_check` 与 `pytest --collect-only` 输出为准 |
| `docs/competition/presentation-closure-report.md` §5 | 写死 521 项测试 | 同上；该报告是历史记录，数字应标注"当时口径" |
| README 验证状态 | 测试数/指纹 | 同上（每轮由 `scripts/git_sync.ps1` 前的自检确认） |

> 处理规则（来自 `AGENTS.md`）：对外数字由**单一 owner 一次改全**；发现漂移只报告或改一行就停，不批量改。

## 3. "设计中"两项的真实阻断点（这是回答"为什么还不算完成"的关键）

**A. 多智能体协同**——代码与测试已有，缺三件：
1. `collab_run` 未注册为 MCP 工具（当前 MCP 仍是 14 个工具）；
2. 未在 Nexent 平台真机调用（现只有离线确定性跑通）；
3. 未进入演示脚本——因为进了会让"结论一致性"多一个需要现场解释的层次。

**B. 定量层**——公式与测试已有，且 [P12 场景 v2 口径标定报告](../reports/p12-scenario-v2-calibration-report.md) 已经算出**换口径的真实代价**：
按 v2 字面替换（来水 vs 可用能力）会把移动泵缺口从 **2 台变成 0 台**，缺口为 0 则 C3 资源争用冲突消失、"跨片区先给谁"这个 P9 决策对象失去事实基础，
12 个分配案例与全部进化数字都要重标。该报告给了两个方案：**A（推荐）需求折算保留 v1，负荷用 `load_state` + 二次风险表达；B 字面替换并整批重标**。
这条口径**需要用户裁决后才能接六跳**。另缺三件**台账字段**（缺了就只能假精确）：
1. 泵站服务范围的**汇水面积**（当前台账只有设计流量 3.09 m³/s 的一次性报告，没有服务面积）；
2. 泵站的**故障泵组能力明细**（现在只有"1# 泵组故障"的文字描述，800 m³/h 是推算的）；
3. 内涝点的**积水面积**（现在只有深度 30 cm，没有面积，`dV/dt` 无法算到时时刻）。

补法：在台账里加三列（汇水面积 / 故障能力明细 / 积水面积），标注"假设值，待确认"，再让定量层接管跳 5 的缺口口径。
**在此之前，六跳的缺口数字必须继续标"v1 口径"，PPT 不得写成"物理模型算出来的缺口"。**

## 4. 优先级与时间（人日估算，按"答辩能不能过"排序）

| 优先级 | 事项 | 工作量 | 前置 | 做完的标志 |
| --- | --- | --- | --- | --- |
| P0 | 套官方模板 1 → 导出可提交 PDF | 0.5 人日 | 拿到官方模板原文（用户侧） | 模板逐项打勾 + PDF 落盘 |
| P0 | 业务价值 15 分钟实测 + 回填 | 0.5 人日 | 3 人到位 | `value-evidence.md` §2 全部改成实测 |
| P0 | 3–5 分钟问答录屏 | 0.5 人日 | 平台在线 | 视频文件 + 与离线 8/8 对应 |
| P1 | 3 个事件真机回归 | 0.5 人日 | 平台在线 | 3 份 id + 逐条比对表 |
| P1 | 初赛 PPT（10 页内） | 1 人日 | P0 前三项 | 用主线文档取话术，不另写 |
| P1 | 真实用户评分 3–5 人 | 1 人日（含组织） | 用户侧找人 | `reviews/` 有数据 + 汇总 |
| — | 公网 Demo（**已完成** 2026-11-04） | — | — | 发布通道 WorkBuddy SitesDeploy；复验记录见 §1"公网 Demo 地址"行 | 外网可开 `/`、`/evolution`、`/api/llm-decision`、`/api/platform-evidence` 四个端点 |
| P2 | 定量层接管跳 5 缺口（含数字一次改全） | 2–3 人日 | §3 三个台账字段 | 六跳缺口可公式复算 + changelog |
| P2 | `collab_run` 注册 + 平台真机 | 1–2 人日 | 平台在线 | 平台调用日志含 `collab_run` |

## 5. 本轮核对记录（命令与实测输出，2026-11-02）

| 核对项 | 命令 | 实测输出 |
| --- | --- | --- |
| 六跳推演 | `python -m decision_poc.deduction --event WO-20261029-007 --as-of 2026-10-29T09:40:00+08:00` | 六跳全 supported；冲突 C1/C2/C3 全命中；资源缺口 "需 8 台 / 可用 6 台 / 缺口 2 台"；处置顺序 10 条；二次风险 5 条；明确列出 2 条"演示假设"与 2 条"需人工接手" |
| 反事实 CF1 | `python -m decision_poc.counterfactual --case CF1` | "J-01→J-02 从 820 m / 1.2 分钟 变为 3860 m / 6.3 分钟（+3040 m，+5.1 分钟）"；并声明"门到门到场时间/跨区路径无法计算" |
| 评测用例 | `python -m decision_poc.urban_eval` | "用例 22/22 通过；六跳 100%，引用完整 100%，检索命中 100%；资产解析 19/19" |
| 策略进化 | `python -m decision_poc.policy_evolution` | "训练 0.2264→0.0000；留出 0.2182→0.0000；轮次 3（采纳 2 / 拒绝 1）；第二会话起点 0.0000、轮次 1；真实事件 A-03 4→5、B-01 2→1" |
| 对外数字自检 | `python -m decision_poc.evidence_check` | "108/108 项一致（期望指纹以命令输出为准）" |
| 产物存在性 | `Get-ChildItem` | `LICENSE`(MIT)、`docs/competition/初赛作品信息收集表-内容稿-v0.1.docx`、`docs/reports/captures/` 9 个证据文件均在；**全仓无 `.pptx`**；**无 `reviews/` 数据** |
| 平台环境 | `docker ps` + 端口探测 | Nexent 全栈在线（northbound/elasticsearch/postgresql/web 等 Up）；`127.0.0.1:8787` 与 `:3000` 可连 |
| ActionCard 字段 | 读 `src/decision_poc/collab/contracts.py` | 8 项里实现 4 项（责任部门经 `agent_id`→`ROLES` 解析、动作、证据来源、联合签发）；缺 4 项（最晚执行时间、所需资源绑定、前置条件填充、验收口径） |
| 场景 v2 标定 | 读 `docs/reports/p12-scenario-v2-calibration-report.md` | 倒灌口径可落地（堰流 2,326 m³/h）；泵站按 v2 字面替换会把缺口 1→0 并摧毁 C3/P9 叙事，报告已固化为测试，**替换方案待用户裁决** |

> 说明：以上输出取自本轮工作区状态。测试总数与自检指纹每轮可能变化，**引用时一律以命令输出为准**，不在文档里写死。

## 6. 一句话结论

技术证据已经够厚，**当前最快的得分点不在写代码，而在把已有证据变成评委看得懂的产物**：
套版、录屏、实测业务价值、真机多事件。定量层与多智能体协同继续保持"设计中有代码"的诚实口径，
等主链路交付物补齐后再决定是否投入 2–3 人日做数字重算。
