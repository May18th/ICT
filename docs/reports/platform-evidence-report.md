# 平台侧证据报告：技能仓库上传 + 平台界面截图

- 日期：2026-11-02
- 环境：本机 Docker 部署 Nexent **v2.7.0**（`nexent-config` 5010、`nexent-web` 3000、`nexent-northbound` 5013）
- 目的：把"基于 Nexent 可运行"里此前只差**平台侧可见证据**的两项补齐——技能是否真在平台技能仓库里、
  平台界面是否能截到我们宣称的东西——并且让这两项**可核对、可重跑、漂移即失败**，而不是 PPT 里的两张图。

## 1 结论

| 项 | 结果 | 可核对证据 |
| --- | --- | --- |
| 2 个 SKILL.md 上传到平台技能仓库 | ✅ `skill_id=2`（`scarcity-allocation-evolution`）、`skill_id=3`（`urban-six-hop-deduction`），`source=custom` | `samples/nexent/live/skills.json`（本地文件与平台正文两份 sha256，`content_matches=true`） |
| 重复执行幂等 | ✅ 第二次运行三个技能全部 `unchanged`，不发写请求 | 同上 `skills[].action` |
| 平台界面截图 3 张，每页带断言 | ✅ 技能仓库 / 智能体列表 / MCP 连通性校验，三页全部命中期望文字 | `docs/reports/captures/nexent/{skill-space,agents,mcp-space}.png` + `platform_ui.json` |
| 截图与技能证据纳入对外数字自检 | ✅ `evidence_check` 新增 `platform_skills` / `platform_ui` 两组共 9 项，当前 **106/106 项一致** | `python -m decision_poc.evidence_check` |

## 2 上传实现与幂等口径

- 入口：`deploy/nexent/upload_skills.py`（容器内运行，走平台自己的 `POST /api/skills/upload`，
  单 `SKILL.md` 由平台 `SkillLoader` 解析，正文由平台落盘）。
- 鉴权：平台管理 API 用 JWT（HS256，`SUPABASE_JWT_SECRET`），密钥只在 `nexent-config` 容器里，
  统一由 `deploy/nexent/platform_client.py` 签发（与 `setup_nexent.py` 共用，不在宿主机伪造鉴权）。
- 幂等判定：平台已有同名技能且正文/描述/tags 完全一致 → `unchanged`；内容漂移 → `PUT /api/skills/<name>/upload`
  → `updated`；不存在 → `POST /api/skills/upload` → `created`。**重复执行不产生无意义写入**。
- 复核：上传后再 `GET /api/skills` 取回平台存储正文，与本地 SKILL.md 正文逐字节比 sha256，
  `all_verified=true` 才算成功（退出码 0）；缺技能或正文不一致退出码 1。

实测输出（首次）：

```text
[skills] 目标目录 /tmp/nexent_skills，待处理 2 个技能；平台已有 ['ontology-builder']
[skill] scarcity-allocation-evolution: created (http=201 skill_id=2)
[skill] urban-six-hop-deduction: created (http=201 skill_id=3)
[skills] 平台复核：['ontology-builder', 'scarcity-allocation-evolution', 'urban-six-hop-deduction']；缺失 []；正文逐位一致=True
```

重复执行：

```text
[skill] scarcity-allocation-evolution: unchanged (http=None skill_id=2)
[skill] urban-six-hop-deduction: unchanged (http=None skill_id=3)
```

## 3 界面截图实现

`deploy/nexent/capture_platform_ui.py`（宿主机，Playwright + 本机已装 Edge，不额外下载浏览器）：

| 页面 | 断言（必须出现在页面文字里） | 产物 |
| --- | --- | --- |
| `/zh/skill-space` →「我的 Skill」 | `urban-six-hop-deduction`、`scarcity-allocation-evolution` | `skill-space.png` |
| `/zh/agents` | `城市内涝跨部门决策智能体` | `agents.png` |
| `/zh/mcp-space` →「我的MCP」→ 连通性校验 | `urban-twin-mcp`、`13 个工具`、`连接成功` | `mcp-space.png` |

- 任一断言不成立即退出码 1，截图不能冒充通过；
- `platform_ui.json` 记每页 URL、命中/缺失文字、PNG 字节数与 sha256；
- 加 `--video` 会额外录一段页面浏览 `.webm`（**不是问答全过程录屏**）。

## 4 防漂移：进了对外数字自检

`samples/eval/evidence_expectations.json` 新增两组期望，`evidence_check` 反向核对：

- `platform_skills`：技能数 2、名称、`local_file_hashes_match`、`platform_content_matches`。
  **改了 `samples/nexent/skill/*/SKILL.md` 却没重新上传，本地文件 sha256 与证据不符 → 自检失败**；
- `platform_ui`：页面数 3、`all_expectations_met`、`screenshot_hashes_ok`。
  **截图被替换/重新截过却没重生成证据 → 自检失败**。

## 5 权限与凭据边界（实测踩到的）

- `suadmin@nexent.com`（平台总管理员）只能进「资源管理」，进 `workbench` 系列页面会报**无权限访问**；
  截图必须用**租户用户**（`admin@nexent.com`，即 `urban_deduction_agent` 的创建者）。
- 口令只从环境变量 `NEXENT_ADMIN_EMAIL` / `NEXENT_ADMIN_PASSWORD` 读，脚本不打印、不入库；
  本轮曾按用户确认重置过一次租户管理员口令（平台状态变更，已当面告知新口令，未写入仓库）。
- 界面里 MCP 卡片的"未连接"是**上次校验的快照**（容器重启后不会自动重测）；截图脚本会先点一次
  「连通性校验」再取证，所以图里是"连接成功"。

## 6 诚实边界

1. 技能只是**上传到平台技能仓库**，还没有绑定到已发布的 `urban_deduction_agent`
   （平台要求 Agent 处于草稿态才能建技能实例，`/api/skills/instance/update`）；
   **因此不宣称"平台 Agent 会调用这两个技能"**。
2. 截图与技能证据都来自**本机 Docker 单环境**；换机器必须重跑并重生成证据，否则 `evidence_check` 会失败。
3. 录屏只覆盖页面浏览；问答过程录屏（提问 → 平台调工具 → 六跳 JSON 出现在界面）仍待补，
   本轮问答一致性证据仍是 `samples/nexent/live/`（conversation_id=21，8/8）。

## 7 复现命令

```powershell
# 1) 技能上传（幂等）+ 证据落盘
docker cp deploy/nexent/platform_client.py nexent-config:/tmp/platform_client.py
docker cp deploy/nexent/upload_skills.py    nexent-config:/tmp/upload_skills.py
docker exec nexent-config sh -c "rm -rf /tmp/nexent_skills"
docker cp samples/nexent/skill nexent-config:/tmp/nexent_skills
docker exec nexent-config sh -c "cd /opt/backend && .venv/bin/python /tmp/upload_skills.py all --json /tmp/skills_evidence.json"
docker cp nexent-config:/tmp/skills_evidence.json samples/nexent/live/skills.json

# 2) 平台界面截图（需要租户账号口令，只放环境变量）
$env:NEXENT_ADMIN_EMAIL    = "admin@nexent.com"
$env:NEXENT_ADMIN_PASSWORD = "<平台密码>"
uv run --with playwright python deploy/nexent/capture_platform_ui.py --out docs/reports/captures/nexent

# 3) 核对：两组平台证据 + 全部对外数字
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.evidence_check
# 期望：对外数字自检：106/106 项一致
```

离线单测（不连平台、不发请求）：`uv run --locked pytest tests/test_nexent_skill_upload.py -q`（13 项）。
