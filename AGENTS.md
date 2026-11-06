# AGENTS.md

## 项目定位

- 本仓库是通用、离线、非生产用途的可进化决策智能体 PoC。
- 当前不绑定行业，不接入真实 Nexent、真实模型、数据库或生产副作用。

## 架构约束

- LangGraph 是唯一流程编排器。
- Pydantic AI 只负责类型化决策生成；当前使用 `TestModel`。
- LangChain Core 只作为 Runnable 演示/评测边界，不建立第二套 Agent Loop。
- 新增行业逻辑、外部系统接入或依赖替换前，必须先更新设计文档并获得明确确认。

## 开发与验证

- 依赖版本以 `pyproject.toml` 和 `uv.lock` 为准，Python 版本为 3.12。
- 代码变更必须配套测试；公共行为变化必须同步更新 README 或 `docs/`。
- 交付前运行：

  ```powershell
  uv run --locked pytest -q
  uv lock --check
  uv run --locked python -m compileall -q src tests
  uv pip check --python .venv\Scripts\python.exe
  ```

- 每轮工作结束必须提交并推送进度（见下节"进度上传"）；`codex/` 保存本地交接状态，默认不提交到 Git；重大修改前先阅读 `README.md`、本文件和相关设计文档。

## 进度上传（每轮工作结束必做）

- **用户已在 2026-11-02 明确授权本仓库直接提交并推送**，覆盖此前"未经要求不推送"的默认限制；
  仍然不建 PR、不改远程分支历史、不强推。
- 一轮对话/工作结束时执行（本机只有 Windows PowerShell 5.1，没有 pwsh 7）：

  ```powershell
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/git_sync.ps1 -Message "<type>(scope): 中文摘要>"
  ```

  脚本行为：无改动直接退出 0（不产生空提交）→ 有改动则**只暂存已跟踪文件**（`git add -u`）+ 提交
  → 推送到 `origin` → 打印分支与提交号。失败时退出码 1，**不要强推**，先看错误。
- **禁止 `git add -A` / `git add .`**（2026-11-06 事故来源：演示脚本刚生成的 `variant.csv` 被一起提交）。
  规则三条：① 临时脚本、变体、演示产物一律写到 `artifacts/`（已忽略），跑完不必删也不入库；
  ② 新增文件与并发场景一律显式路径 `-PathList "a,b,c"`；③ 提交前 `git status --porcelain` 人工过一眼，
  untracked 项非 0 就停下来查清是谁的。
- 提交信息用 `<type>(<scope>): <摘要>`，type 取 `feat|fix|docs|test|chore`；一次提交讲清一件事。
- 推完在回传里记录：分支名 + `git log -1 --oneline`（远程操作必须可追溯）。
- 不入库：`.venv/`、`artifacts/`、`codex/`、`.worktrees/`、`pytest-of-*/`、口令与密钥
  （`.gitignore` 已列；新增忽略项要写进 `.gitignore` 而不是每次手工挑文件）。
- 大二进制（UE 资产）照常入库；单文件接近 50 MB 前先与用户确认。
- 若工作由多个代理并发进行（同一工作区），提交前先 `git status` 确认没有别人正在写的半成品；
  有冲突就只提交自己负责的路径，并说明未提交的部分：

  ```powershell
  powershell -NoProfile -ExecutionPolicy Bypass -File scripts/git_sync.ps1 `
      -Message "docs: xxx" -PathList "AGENTS.md,docs/competition/technical-breadth-and-presentation-boundary.md"
  ```

## 对外数字：单一 owner + 一次性同步五处

对外数字（测试数、自检项数、指纹、各项指标）是冻结口径，漂移会直接毁掉"可核对"。改它之前先确认：

1. **同一时间只由一方修改共享数字文件**：`samples/eval/evidence_expectations.json`、`README.md`、
   `docs/competition-gap-audit.md`、`docs/reports/evidence-check-report.md`、`scripts/reproduce.py`
   里的 `expect` 正则。其他代理发现漂移只报告或改一行就停，不批量改。
2. **一次改全，不留半套**（新增自检项/改测试数时要同时同步）：
   - 期望值 `samples/eval/evidence_expectations.json`（含 `checks.*`、`test_baseline.collected`、`docs_declared`）；
   - 三份文档测试数：`README.md`、`docs/competition-gap-audit.md`、`docs/competition/preliminary-submission-draft.md`；
   - 所有"`X/X` 项一致"与期望指纹：README、`docs/demo-runbook.md`、
     `docs/competition/technical-breadth-and-presentation-boundary.md`、
     `docs/competition/presentation-closure-report.md`、`docs/innovation-and-evidence.md`、`docs/README.md`、
     各 design/spec 文档、`scripts/build_preliminary_docx.py`；
   - `scripts/reproduce.py` 的自检 `expect`（写死 `X/X`）与步骤数；
   - 重新生成 `docs/reports/evidence-check-report.md`（勿手改）。
3. **验收只有两条**：`git status` 干净 + `python -m decision_poc.evidence_check` 输出当前口径
   （当前 `106/106`，指纹 `6a9030396892c48f`）。**不看文档里的旧数字**，也不允许为过检放宽期望。

全仓扫旧数字时注意 `Get-ChildItem -Recurse`：`docs/**/*.md` 这种通配只匹配一层，
`docs/superpowers/specs/` 这类深目录容易漏扫。

## 运行 reproduce 后的噪声处理

`scripts/reproduce.py` 会重写 `docs/reports/p8-evaluation-report.md` 与 `samples/eval/latest.json`，
里面含 `p50/p95` 与逐用例毫秒计时，**必然产生随机 diff**。除非本轮就是要更新这两份报告，否则跑完
`reproduce` 先恢复它们再提交：

```powershell
git checkout -- docs/reports/p8-evaluation-report.md samples/eval/latest.json
```

`git status` 干净后再 `git add`，避免把计时抖动带进提交。
