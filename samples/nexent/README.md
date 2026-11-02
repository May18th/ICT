# Nexent 侧绑定表

`skills.json` 是 Nexent 编排需要的全部信息：五个 Skill 的调用顺序、请求参数结构、各自绑定的 MCP 工具、失败时的语义。生成方式：

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.skills --manifest > samples/nexent/skills.json
```

**不要手改这个文件。** 它由 `src/decision_poc/skills.py` 里的 `skill_manifest()` 在运行时从同一批 Skill 对象导出，改代码后重新生成即可。手改会和代码漂移，而漂移只会到联调时才暴露。

## 这里有什么、没有什么

| 有 | 没有 |
| --- | --- |
| Skill 名称、阶段、执行顺序 | 任何业务规则（规则在 `samples/rules/`） |
| 请求 JSON Schema（可直接做参数校验） | Prompt 文案（属 Nexent 侧，随场景调整） |
| 每个 Skill 依赖的 MCP 工具名 | 工具实现（在 `src/decision_poc/mcp_server.py`） |
| 失败模式说明（`failure_mode`） | 密钥、地址、部署配置 |

反事实（`counterfactual_run`）不在 `skills` 列表里：它不是一个链路步骤，而是对已发布结论的追问。所以它单独放在 `standalone_tools`，由编排层在拿到基线结论后调用，用例编号为 CF1–CF3。

设计口径：**代码里能测的放本仓库，只有措辞和部署参数放 Nexent。** 因此 `skills.py` 里的五个 Skill 是可被 pytest 覆盖的实现，`skills.json` 只是它们的对外描述；Nexent 侧配置写成什么样，都不改变它们的判定结果。

## 工具名不是随便写的

`test_manifest_only_names_tools_that_exist` 会用 `UrbanToolkit.tools()` 校验 `mcp_tools` 里每个名字都真实存在。绑定表里写错一个工具名，测试就会失败——而不是等 Nexent 跑到那一步才发现。

## 进程内外的差别

`skills.py` 在同一进程里直接调用语义层与推演引擎，不为每次调用起一个 MCP 子进程（16 个资产规模下 IPC 纯粹是额外故障点）。`mcp_tools` 字段描述的是**外部编排时应绑定的工具**，两者结论必须一致，`test_skill_deduction_agrees_with_the_mcp_tool` 逐项比对冲突编号、资源缺口、处置顺序与快照哈希来守住这一点。

## 真正接进 Nexent：SSE + SKILL.md（P9.3）

上面这些是"描述"，Nexent 实际只认两样东西：

1. **MCP 服务必须是 HTTP/SSE**（远程链接 / 容器 / API 转 MCP），stdio 进程接不进去：

   ```powershell
   $env:PYTHONPATH = "src"
   uv run --locked python -m decision_poc.mcp_sse --port 8000
   # MCP SSE：http://127.0.0.1:8000/sse；/health 可查 13 个工具
   ```

   Nexent 里填 `http://host.docker.internal:8000/sse`（Docker 部署访问宿主机）。

2. **Skill 用 SKILL.md 上传**，本目录 `skill/` 下是两个可直接上传的 SOP：
   - `skill/urban-six-hop-deduction/SKILL.md`：六跳编排 + 证据要求 + 失败语义；
   - `skill/scarcity-allocation-evolution/SKILL.md`：稀缺分配 + 进化前后对比 + 回滚口径。

   已实测上传到本机 Nexent 技能仓库（`skill_id=2/3`），幂等脚本与命令见
   [deploy/nexent/README.md](../../deploy/nexent/README.md) §2.1：
   `deploy/nexent/upload_skills.py all --json /tmp/skills_evidence.json`，
   证据落 `samples/nexent/live/skills.json`（含本地文件与平台正文两份 sha256，`content_matches=true`）。
   平台界面截图见 `docs/reports/captures/nexent/skill-space.png`。
   边界：技能在仓库里可浏览，**未绑定到已发布的 `urban_deduction_agent`**（绑定要草稿态 Agent）。

真机验证用 `python -m decision_poc.nexent_urban`（8 项与离线结论比对，不是"返回 JSON 就算过"）。
完整步骤、证据清单与验收见 [集成设计](../../docs/nexent-urban-integration-design.md)。
