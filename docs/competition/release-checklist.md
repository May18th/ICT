# 开源与提交清单（P1：让评委复现得出来）

> 目的：把"代码在本地能跑"变成"别人干净 clone 后按 README 能跑出同样数字"。
> 状态以本文件为准；每完成一项就改状态，不要只在聊天里说"已经做了"。

## 1. 已完成（本轮）

| 项 | 结果 | 核验方式 |
|---|---|---|
| 开源许可 | 新增 `LICENSE`（MIT，著作权人 `May18th`，如需改成队伍/学校名称只动这一行） | `git check-ignore -v LICENSE` 应无输出（即未被忽略） |
| 一条命令复现 | 新增 `scripts/reproduce.py`，17 步覆盖测试/数据集/布局/推演/反事实/Skill/P8/MCP/P9/P11 消融与锚定/106 项自检 | `uv run --locked python scripts/reproduce.py`，期望 `17/17 步通过` |
| 平台侧证据 | 2 个 SKILL.md 上传 Nexent 技能仓库（skill_id=2/3、幂等、正文逐位一致）；平台界面 3 张截图 + `platform_ui.json`（sha256 进自检） | `evidence_check` 的 `platform_skills` / `platform_ui` 两组；报告 [平台侧证据报告](../reports/platform-evidence-report.md) |
| UE 场景可被智能体驱动 | `UrbanTwinDemo.uproject` 启用 `ModelContextProtocol` / `ToolsetRegistry` / `EditorToolset`，`Config/DefaultEditorPerProjectUserSettings.ini` 写入 8000 端口自动启动 | 启动编辑器后 `http://127.0.0.1:8000/mcp` 可初始化 |

## 2. 已推送的提交分组（2026-11-02，`main` = `6f7c559`）

按"一次提交一个可解释的单元"分组，顺序即建议提交顺序：

| # | 提交信息 | 文件 |
|---|---|---|
| 1 | `chore: add MIT license` | `LICENSE` |
| 2 | `chore: one-command reproduction script` | `scripts/reproduce.py`、`.gitignore`（忽略 `*.bak-mcp` 与 `artifacts/`） |
| 3 | `feat(p10): visualization tools and UE bridge` | `docs/p10-visualization-tools-design.md`、`src/decision_poc/visualize.py`、`scene_layout.py`、`scene_export.py`、`web_demo.py`、`mcp_server.py`、`samples/assets/urban/scene_layout.json`、`tests/test_web_demo.py`、`tests/test_mcp_server.py` |
| 4 | `feat(p9): decision evolution loop` | `src/decision_poc/allocation.py`、`policy_evolution.py`、`experience.py`、`docs/superpowers/specs/2026-10-30-p9-decision-evolution-design.md`、`samples/eval/allocation_cases.json`、`decision_reviews.json`、`decision_memory.json`、`docs/reports/p9-policy-evolution-report.md` |
| 5 | `feat(nexent): real-machine integration` | `deploy/`、`docs/nexent-urban-integration-design.md`、`docs/nexent-tool-registration.md`、`docs/reports/nexent-live-integration-report.md`、`src/decision_poc/nexent_urban.py`、`mcp_sse.py`、`samples/nexent/live/` |
| 6 | `feat(semantic): deduction and counterfactual fixes` | `src/decision_poc/deduction.py`、`skills.py`、`urban_eval.py`、`samples/eval/urban_cases.json`、`samples/rules/`、`tests/` |
| 7 | `docs: competition materials` | `docs/competition/`、`docs/competition-gap-audit.md`、`docs/reports/evidence-check-report.md`、`docs/innovation-and-evidence.md`、`README.md`、`docs/README.md` |
| 9 | `feat(nexent): upload skills and capture platform ui` | `deploy/nexent/platform_client.py`、`deploy/nexent/upload_skills.py`、`deploy/nexent/capture_platform_ui.py`、`samples/nexent/live/skills.json`、`docs/reports/captures/nexent/`、`docs/reports/platform-evidence-report.md`、`tests/test_nexent_skill_upload.py` |
| 8 | `feat(ue): enable unreal mcp plugins` | `visualization/unreal/UrbanTwinDemo.uproject`、`visualization/unreal/Config/DefaultEditorPerProjectUserSettings.ini`、`visualization/unreal/README.md` |

提交前必须满足（`agent_poc/AGENTS.md` 的交付门禁）：

```powershell
uv run --locked python scripts/reproduce.py     # 17/17
uv lock --check
uv run --locked python -m compileall -q src tests
uv pip check --python .venv\Scripts\python.exe
```

## 3. 提交前必须先确认的三件事

1. **评测报告与基线的一致性**：本轮重跑 `urban_eval` 后，`docs/reports/p8-evaluation-report.md` 的"多跳完整"从
   63.64% 变为 100.00%，与 README 宣称的 100% 一致；但"改进前后对比"表因此变成 100%→100%。
   若答辩要讲"改进前 86.36%"，需先用 `samples/eval/make_pre_guard_baseline.py` 重新冻结基线，
   再同步 README、报告与答辩口径（仓库规则：改数字必须同步期望文件，不允许为过检放宽期望）。
2. **`.uecheck/` 不要提交**：本地 UE 探针产物，加进 `.gitignore` 或删除。
3. **`visualization/unreal/Content/UrbanTwin/Maps/UrbanTwinDemo.umap`** 是编辑器生成内容，确认是否要作为
   可复现材料提交；若提交，README 需说明首次打卡即可，不需要评委自己建关卡。

## 4. 推送（用户已授权，2026-11-02 已推送）

`AGENTS.md` 新增"进度上传"一节：用户已授权直接提交并推送，**每轮工作结束执行**
`powershell -NoProfile -ExecutionPolicy Bypass -File scripts/git_sync.ps1 -Message "<type>(scope): 摘要>"`；
脚本无改动不空提交、失败不强推、打印分支与提交号。

本轮实测（2026-11-02）：12 个分组提交推到 `origin/main` = `6f7c559`，此后仅一条文档状态提交；`main` 与 `origin/main` 保持同步，工作区干净。当前 HEAD 以 `git log -1 --oneline` 为准，下面只列分组内容：

```text
9339179 feat(nexent): 上传 SKILL.md 到平台技能仓库并固化平台界面证据
2a884ee feat(p9): 决策进化闭环、分层记忆与真实用户评分通道
6a121fc feat(p10): 可视化工具层、实体重投影与回放数据
322b4e9 feat(p11): 消融阶梯与决策锚定包
1e49200 feat(eval): 图像语义抽取与真实模型 A/B 评测
8748dcd feat(nexent): 北向传输、MCP-over-SSE 与真机集成
06f30bb feat(ue): 关卡、真实资产与按 RenderJob 高亮驱动
2471361 chore(repo): 一键复现脚本、MIT 许可与依赖锁更新
40dbbf2 docs: 对外材料与数字口径同步
f0c9c2c chore(repo): 每轮工作结束自动提交推送规则
7494e44 chore(repo): 修正自动上传命令为 Windows PowerShell 5.1 可执行形式
6f7c559 chore(repo): 让 git 同步脚本在 PowerShell 5.1 下不把警告当异常
```

`docs/competition-gap-audit.md` 第 3 节"完整代码 + 数据集"与第 4 节 Git 行已同步为 `6f7c559`。
