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

- 未经用户明确要求，不推送、提交 PR 或修改远程仓库；远程操作完成后记录分支和提交。
- `codex/` 保存本地交接状态，默认不提交到 Git；重大修改前先阅读 `README.md`、本文件和相关设计文档。
