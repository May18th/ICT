# 赛题 3 差距审计：技术自洽之外，我们漏了什么

- 日期：2026-10-30
- 目的：把"官方要什么"与"我们有什么"逐条对齐，找出会直接影响晋级/评分的硬缺口。
- 事实来源：`D:\华为ICT\.workbuddy\memory\2026-10-29.md`「赛题要求核对」小节、`docs/ICT-创新赛-赛题3-方案与实施计划.md` 阶段 6、本仓库可运行状态实测。
- **2026-11-06 已补齐官方原文**：官方报名页 + 官方赋能直播（BV18Kxv6jEuF，31:16）+ Nexent 源码核对，结论沉淀在 [官方口径落库](competition/official-brief-2026-11-06.md)，分段时间轴见 [直播要点](competition/nexent-topic3-brief-transcript.txt)。下方原 `[待官方复核]` 项已据原文改写。
- 标注规则：`[已核实]` = 本仓库/本机实测或官方原文可验证；`[已核实·官方页]` = 官方报名页原文；`[已核实·直播 mm:ss]` = 官方赋能直播原话；`[推断]` = 未核实，不得写进答辩材料。
- **配套（2026-11-04 补）**：本文件只审"五个考点能不能自证"；**"能不能赢"另见 [可交 vs 可赢：竞争力审计](competition/winning-gap-audit.md)**
  （结论：能交不远，能赢还早，缺口在真实性、可验证的进化实验、业务价值与 3 分钟叙事，不在代码）。

## 0 一句话结论（2026-11-06 更新）

**技术内核已经不是问题，问题在"交得出手"**：五条考点都有可核验证据、真机 8/8 已打通；
今天补齐官方原文后，**新增 4 个此前没识别出的交付缺口**——评测数据集、平台导出包、离线部署包、平台特性加分项。
其中前两项动作很小（导出 + 整理，各半天内），后两项工量中等但属评委会问的项。

一句话：**距 12 月初赛材料截止还有约 4 周，真正卡进度的不是技术，是"还没按官方要的形态把已有东西装箱"。**

## 1 官方口径（2026-11-06 已对官方原文核实）

| 项 | 内容 | 状态 |
| --- | --- | --- |
| 赛道 | 第十一届华为 ICT 大赛 创新赛 赛题 3「基于 ModelEngine / Nexent 打造可进化决策智能体」 | `[已核实·官方页]` |
| 五个考点 | ① 多模态数据资产认知（≈元数据构建）② 低资源知识图谱本体半自动构建 ③ MCP / Skills 编排 ④ 跨文档多跳推理与可溯源 ⑤ **基于 Nexent 可运行** | `[已核实·官方页]` |
| 初赛提交 | **模板 + Agent 构建 + Skills/MCP 设计 + 评测（需评测数据集）+ 平台导出包 + 录屏演示**（比原认知多两项：评测数据集、导出包） | `[已核实·直播 16:21]` |
| 决赛提交 | 图谱设计文档、场景具化、业务落地价值、完整答辩材料、Demo 地址、代码、数据集、README、复现脚本、开源截图；**评委现场复现核验** | `[已核实·直播 17:16]` |
| 赛程 | 报名 2026-08-31 ~ 11-30；初赛 2026-12；中国总决赛 2027-03；**赛题 3 无全球赛** | `[已核实·官方页]` `[直播 30:23]` |
| 队伍 | 3 名学生 + 1 名指导教师，同校，每人限 1 个赛道；队长建队、队员在线确认、3 人全确认才算报名完成 | `[已核实·直播 19:00]` |
| 离线部署 | 平台作品须支持**离线部署包**，不能强依赖在线环境 | `[已核实·直播 14:38]` |
| 三方依赖 | 必须用友好开源协议（MIT 等），不得侵犯第三方著作权 | `[已核实·直播 00:47]` |
| 加分项 | **对平台能力做优化 / 提交有价值特性**，属重点考察加分项 | `[已核实·直播 15:06]` |
| 算力券 | **初赛无**；中国总决赛阶段发放（约 1 月上中旬） | `[已核实·直播 26:36 / 29:46]` |

风险提示：规则层已全部落到官方原文/原话，**「记错规则」的风险解除**；剩余风险只在执行——材料形态、复现核验、评分证据。

## 2 五个考点逐条对照

| 考点 | 我们的证据 | 缺口 | 风险 |
| --- | --- | --- | --- |
| ① 多模态数据资产认知 | 19 个资产（17 合成 + 2 真实公开预案）统一登记：`asset_id`、sha256、locator、版本、有效期、权限；P8 资产解析 19/19、未识别列 0；**图像已做像素级语义抽取**（`decision_poc.image_semantics`：频谱峰值 120/240/360/480Hz、示意色块走向、积水范围 bbox，可转 RegionSpec 引用）`[已核实]` | 仍**没有 OCR / 视觉模型**：图例文字含义需调用方映射，频率轴范围来自生成器契约 | 低（原中） |
| ② 低资源 KG 本体半自动构建 | 种子本体 → 规则抽取 → 置信度分档 → 人工确认 → 版本发布/回滚；P3 报告实测（改 1 格 → 2 条候选、回滚哈希逐位一致）`[已核实]` | 候选抽取是**规则+词典基线**，没有与 LLM 抽取对比的"低资源"量化（少样本 vs 全监督） | 低 |
| ③ MCP / Skills 编排 | **平台注册** 15 个 MCP 工具（决策 10 含 `doc_search`/`allocation_plan`、呈现 3、引导 1、协同 1），平台 `/api/tool/list` 实测 15/15 全为 `source=mcp`、`is_available=true`；**检索半边已补**：`doc_search` 对台账登记的 19 个业务资产建混合索引（`rag build --registered`），返回的 `evidence_ref` 与 `evidence_locate` 同格式，闭环已验证；**挂载口径须区分**：主 Agent 只挂 2 个（`event_deduce`/`evidence_locate`，`MAIN_TOOLS` 有意收窄，见 `setup_nexent.py:83-89`）、协同 Agent 挂 6 个（`COLLAB_TOOLS`），导出包实测印证（[导出证据](reports/agent-export-evidence.md)）；+ 5 Skill 编排 + 绑定表；**Nexent 真机已由平台 Agent 调用 MCP 工具**（`event_deduce`/`allocation_plan`/`scene_render`，见真机报告）；**2 个 SKILL.md 已上传平台技能仓库**（skill_id=2/3，正文逐位一致，见 `samples/nexent/live/skills.json` + `captures/nexent/skill-space.png`）`[已核实]`；**已绑定到已发布 Agent**：草稿与发布版本各 2 条实例、`present=enabled=true`（`bind_skills.py verify --require-published` 退出码 0，证据 `samples/nexent/live/skill_bindings.json`） | ①`doc_search` **未挂到主 Agent**：主 Agent 的提示词是「给事件号 → event_deduce」两步式，多挂一个工具就会绕过推演（这正是 `MAIN_TOOLS` 收窄的原因），所以它只作为独立检索入口存在；②检索默认剔除**已废止资产**（2021 版预案 `valid_to=2025-06-01`），需要版本对比时显式传 `include_deprecated=true`；③技能正文**当前**不被模型读取（平台只自动注入 `name` + `description`；正文需调用 `read_skill_md`，但主 Agent 提示词已显式禁用该调用，见 `deploy/nexent/setup_nexent.py:96`），所以别说"平台 Agent 按技能文档执行流程" | 低（原"中/高"已降） |
| ④ 跨文档多跳推理与可溯源 | 六跳推演 + 三类冲突 + 反事实 + 哈希链账本 + 变更传播影响分析；引用完整率 100%、篡改可定位 `[已核实]` | 跨部门只覆盖 A-03/B-01 两片区，场景宽度偏窄 | 低 |
| ⑤ **基于 Nexent 可运行** | **已补齐**：Nexent v2.7.0 上注册 15 个 MCP 工具（tool_id 41–53、86），创建并发布 Agent `urban_deduction_agent`（agent_id=3，**v19**，模型 DeepSeek-Chat），真机对话**六跳结论与离线 8/8 一致**（conversation_id=120，2026-11-06 复测），平台调用日志 1 次 `event_deduce` 与本轮一一对应；2 个技能已上传并**已绑定到该 Agent**（草稿 + 已发布版本均 enabled）；平台界面截图三张（技能/智能体/MCP 连通性校验）`[已核实：`samples/nexent/live/` + `docs/reports/captures/nexent/`]` | 仅本机 Docker 单环境、单事件；模型作答协议不稳定（需提示词约束 + 协议层重试）；真实问答录屏仍待补 | 低（原"最高"已解除） |

结论：原"唯一可能一票否决"的考点⑤已补齐（真机 8/8 + 工具调用日志 + conversation_id）。
剩余高风险项转移到**参赛形态**：模板 1 未按官方版式产出、开源截图/复现材料未整备、真实用户评分与 PPT/视频缺失。

## 3 提交物与工程化对照

| 交付物 | 官方要求 | 现状 | 风险 |
| --- | --- | --- | --- |
| 模板 1 作品信息收集表（Word） | 初赛唯一提交物 | **官方模板原文未落库**（只有自制的 .docx 内容稿）；内容稿已生成 `docs/competition/初赛作品信息收集表-内容稿-v0.1.docx`（五考点/数据集处理/关键代码/指标表/演示截图/边界声明，7 页，脚本 `scripts/build_preliminary_docx.py` 可重生成），待按官方版式套版 | 中（内容齐、版式缺） |
| 项目 PPT（初赛） | 覆盖背景价值、创新性、数据集处理、关键代码、量化指标 | **没有** | 高 |
| 模板 2 答辩 PPT（决赛） | 答辩 + 演示 | **没有** | 若进决赛 |
| Demo 地址 | 可访问的演示地址 | 仅 `localhost:8787`（本机启动）；无公网/云上地址 | 中高 |
| 完整代码 + 数据集 | 可复现 | **GitHub origin/main 已推到 `6f7c559`（2026-11-02，12 个分组提交、工作区干净）**；本轮已补 `LICENSE`（MIT）与一键复现脚本 `scripts/reproduce.py`（17 步全过，实测 30 秒内） | 高（复现核验会按 README 跑，跑到旧 commit 数字全对不上）；分组与推送步骤见[开源与提交清单](competition/release-checklist.md) |
| README / 部署说明 | 一键复现 | README 有本地快速开始；**Nexent 部署说明没有**（只有本机 docker 手工步骤） | 中 |
| 复现脚本 | 从干净环境复现 | `pytest` / 各 CLI / `evidence_check` 已有；缺"一条命令生成全部材料"的编排脚本 | 低 |
| 开源截图 | 决赛材料 | `LICENSE`（MIT）已补；仓库公开状态与开源截图待确认 | 中 |
| 演示视频 | 初赛「录屏即可」`[直播 17:05]` | **没有** | 中 |
| **评测数据集 + 评测报告**（2026-11-06 新增） | 初赛要跑基础评测，需评测数据集 `[直播 16:48]` | **已完成说明书级整备**：`docs/competition/eval-dataset-spec.md`——22 例推演集 + 12 例分配集（含 4 例 holdout）字段字典、覆盖分布、五级消融基线、真假数据声明、四条可复跑命令；进化量化（训练 0.2264→0.0000、**留出 0.2182→0.0000**、未满足 15→6 台、记忆复用 1 轮）已落文档。**缺口缩小为**：还没有一份独立成篇的「评测报告」PDF | 低（原中高） |
| **平台导出包**（新增） | 初赛「配置能力」= 平台导出作品 `[直播 17:05]`；实现为 `<agent>.zip` 含 `agent.json` + `skills/*.zip`（`Nexent/backend/management/services/agent/management.py:1410`，接口 `POST /agent/config/export`） | **已完成**（2026-11-06，2026-11-07 重导）：`samples/nexent/live/urban_deduction_agent.zip`（7 982 B）+ `agent_export.json`，脚本 `deploy/nexent/export_bundle.py`；**内容指纹 `0a3aef9c…`**（整包 sha256 不可用，zip 头带导出时刻时间戳，实测 5 次 5 个值），详见[导出证据](reports/agent-export-evidence.md) | 低（已交付，留待套模板时引用） |
| **复现包须含 MCP 服务端**（新增，导出时暴露） | 导出包只存 `mcp_info` 的 server 地址，不含工具清单与实现；评委按 zip 复现时没有 `urban-twin-mcp` 就一个工具都起不来 | 服务端代码在 `src/decision_poc/mcp_server.py` / `mcp_sse.py`，容器启动方式在 `deploy/nexent/README.md`；但**未在材料里显式说明「导出包恢复 Agent 配置、工具能力由 MCP 服务提供」** | 中（一句话说明 + 复现包目录结构即可） |
| **离线部署包 / 离线部署说明**（新增） | 平台作品须支持离线部署，不能强依赖在线 `[直播 14:38]` | **已出评估**：[离线部署评估](competition/offline-deploy-assessment.md)——镜像实测 17.56 GB / 16 个；方案 A 全量 `docker save` 约 17.5 GB（半天工量、保底），方案 B 裁掉 sandbox 有依据（我们全链路禁用 `python_interpreter`，可省 1.89 GB，**待真机验证**）。**仍未产出离线包本身** | 中高（硬要求未满足） |
| **平台特性贡献**（新增，加分项） | 对平台能力优化或提交有价值特性 `[直播 15:06]` | 零准备。可切入口：`read_skill_md` 被提示词禁用（见第 2 节考点③）、MCP 工具参数校验、大资产上传的会话上下文控制 | 机会项 |

## 4 当前环境与可复现状态（实测）

| 项 | 实测结果 |
| --- | --- |
| Docker / Nexent | **可用**：Docker Desktop 启动后 Nexent v2.7.0 全栈在线（`/nb/v1/health` healthy）；MCP 服务以 `urban-twin-mcp` 容器跑在 `nexent_network` |
| Nexent 真机 | **已跑通**：15 工具注册、Agent `urban_deduction_agent` 发布 v19（DeepSeek-Chat）、conversation_id=120、8/8 一致；证据 `samples/nexent/live/` |
| 测试与自检 | 921 项测试通过；对外数字自检 108/108；`uv lock --check`、`compileall`、`uv pip check` 通过 |
| Git | `main` 与 `origin/main` 同步在 `6f7c559`（2026-11-02 推送 12 个提交）；工作区干净；每轮结束由 `scripts/git_sync.ps1` 自动提交推送 |
| 开源合规 | 无 `LICENSE`；`samples/` 为合成数据、来源已标注，未发现真实私有数据；真机证据不含 API Key |

## 5 反馈来源与"用户评分"

赛题把"用户评分"列为合法反馈来源之一。我们目前的人工反馈是**角色扮演评审**（已明确标注非真实批复），
没有真实用户评分记录。评委若追问"谁评的、样本多少、评分分布"，现有材料答不上来。

缺口：至少 3–5 名真实用户（同学/老师/领域从业者）对进化前后方案各打分 + 简短理由，形成 `reviews/` 记录，
作为"第二反馈信号"的实证；角色扮演评审保留为"机制演示"，不要混为一谈。

## 6 应用价值叙事

官方评分通常含应用价值/创新性权重 `[推断，待复核]`。我们的材料几乎全是技术指标
（偏差、引用完整率、延迟），**没有一个"业务侧收益"的量化**。可补的最小证据：

- 同一事件下"人工协同决策耗时"与"智能体推演 + 人工确认耗时"的对照（现场计时即可，标注为估算）；
- 协同顺序错（先封路后隔离）与对的顺序之间的通行代价差（已有 CF1 实测 3040 m / 5.1 分钟，可直接换算）；
- 缺口上报提前量：从"事后发现缺 1 台"到"推演时即报"，对应跨区调运 40 分钟的提前量。

## 7 差距优先级与建议动作

| 优先级 | 动作 | 验收 | 状态 |
| --- | --- | --- | --- |
| P0 | ~~Nexent 真机跑通城市内涝主链路~~ | 六跳 + 分配 + 证据链，与离线可核对 | **已完成**：8/8 + conversation_id=120 + 1 次工具调用日志（[真机报告](reports/nexent-live-integration-report.md)） |
| P0 | ~~拿到官方模板 1/模板 2 与报名通知原文~~ **规则层已解除** | 官方五条任务、赛程、提交物、评审口径已落 [官方口径落库](competition/official-brief-2026-11-06.md) | **已完成（规则层）**；仅剩两件需登录账号下载：报名页的**模板 docx 原件**与「参赛资料要求及评审规则」PDF |
| P1 | 按模板 1 写初赛作品 Word（数据集处理/关键代码/量化指标/演示截图） | 可提交的 PDF/Word | 内容草稿已备：`docs/competition/preliminary-submission-draft.md`，待套版 |
| ~~P1~~ | ~~**导出平台作品包**（新增）：平台 Agent 配置页「导出」→ `<agent>.zip`，核对 `agent.json` + `skills/` 完整，存 `samples/nexent/live/` 并记 sha256~~ | 解压后结构与源码口径一致；初赛「配置能力」项有物可交 | **已完成（2026-11-06，11-07 重导）**：7 982 B / `agent.json` + 2 技能 / 内容指纹 `0a3aef9c…`；顺手澄清「注册 15 工具 vs 主 Agent 挂 2 个」的口径（[导出证据](reports/agent-export-evidence.md)） |
| ~~P1~~ | ~~**整备评测数据集 + 评测报告**（新增）：把 `samples/eval/` 收成「数据字典 + 基线对照表 + 场景覆盖说明 + 一键复跑」~~ | 评委能按 README 复跑出同样的评测数字 | **已完成（2026-11-06）**：`docs/competition/eval-dataset-spec.md`；四条命令全部实测跑通，**并纠正一处**：`decision_poc.evaluation` / `allocation` 没有 `__main__`，真实入口是 `urban_eval` / `policy_evolution` / `ablation` |
| ~~P1~~ | ~~**检索侧接入**（新增）：把 `rag/` 从「能跑但零引用」接成 MCP 工具 `doc_search`，索引按台账登记建，检索结果直接可被 `evidence_locate` 复核~~ | 检索命中 → 取回原文闭环；平台 15/15 工具可用 | **已完成（2026-11-06）**：[接入报告](reports/rag-integration-2026-11-06.md)。索引改 `rag build --registered` 后 `asset_id` 与台账逐位一致；检索默认剔除已废止资产（2021 版预案 `valid_to=2025-06-01`）；主 Agent 仍只挂 2 个（收窄是稳定性设计），检索以独立入口提供 |
| P2 | 平台卫生（新增）：清掉「删 MCP 但不删工具」留下的孤儿记录 | 工具库无 `is_available=false` 条目 | **已完成（2026-11-06）**：删 7 条（6 个 `kg_*` + 1 个 `local_test_tool_name`），平台工具 50→43、不可用项 6→0；备份 `artifacts/platform_tool_tables_backup_20261106.sql` |
| **P2** | **离线部署包**（新增）：`docker save` 关键镜像 + 离线 compose + 离线安装 README；先在小范围（仅 urban-twin-mcp + Nexent 核心）验证断网可起 | 断网机器上能起服务、能跑一次六跳 | **评估已完成**（[离线部署评估](competition/offline-deploy-assessment.md)，16 镜像 17.56 GB，方案 A/B 与裁剪点已列）；**打包未做**，方案 A 约半天，主成本是体积与断网实测 |
| **P2** | **平台特性贡献**（新增，加分项）：至少在 Nexent 社区提交 1 个有价值的 issue/PR | 有 PR/issue 链接 + 采纳记录，可写进材料 | **已提交（2026-11-07）**：[ModelEngine-Group/nexent#4307](https://github.com/ModelEngine-Group/nexent/issues/4307)，主题为「绑定技能时缺技能-工具一致性校验」，含复现路径、量化影响（绑技能 8/8→0/8）、源码定位与最小改动建议。草稿见 [issue 草稿](competition/nexent-issue-draft.md)。**采纳记录待跟进** |
| P1 | 提交并推送当前全部工作；补 LICENSE、部署说明、一键复现脚本 | 干净 clone 后按 README 可复现 108/108 自检 | LICENSE 与 `scripts/reproduce.py`（17/17 步）已完成；**2026-11-02 已推送 12 个分组提交到 `origin/main`（起始点 `6f7c559`）**，每轮结束由 `scripts/git_sync.ps1` 自动提交推送；HEAD 以 `git log -1` 为准 |
| P1 | SKILL.md 上传到 Nexent 技能仓库 | 平台技能列表出现 2 个技能 | **已完成**：skill_id=2/3，正文逐位一致（`samples/nexent/live/skills.json`），平台截图 `captures/nexent/skill-space.png` |
| P2 | 真实用户评分（3–5 人，进化前后各一次） | 采集通道已就绪（`/evolution` 表单 + `python -m decision_poc.review_collect`）；样本待组织（≥3 人且前后各有），汇总与导出命令见 [真实评分通道](real-feedback-channel.md) | 通道就绪，样本待填 |
| P2 | 应用价值量化（三组对照数据） | 一页指标表，进 PPT | 待做 |
| P2 | 平台界面截图 / 演示录屏 | 答辩材料可引用 | **截图已完成**：`deploy/nexent/capture_platform_ui.py` 生成 3 张图 + `platform_ui.json`（带 sha256，自检扣得住）；**问答过程录屏仍待补**（脚本只录页面浏览） |
| P3 | 多模态增强（图片 OCR/语义标注） | 已落地**像素级语义抽取**（`decision_poc.image_semantics`：频谱峰值 / 示意色块走向 / 积水范围 bbox，可转 RegionSpec），10 条测试；仍不做 OCR/视觉模型，图例文字含义需映射 | **已完成（无 OCR）** |

## 8 一句话给答辩定调

不要说"我们做了一个城市数字孪生平台"。
要说"我们在 Nexent 上搭了一个会从反馈里越推越准的决策智能体：推演可回溯、策略可回滚、经验可沉淀"，
并当场打开 Nexent 对话 + `/evolution` 面板，让评委看到**平台上的智能体**与**同一份结论的可视化**。
