# 平台作品导出证据（初赛「配置能力」项）

- 日期：2026-11-06
- 官方依据：初赛需提交「配置能力」，即用平台导出功能导出作品 `[官方赋能直播 17:05]`，来源见 [官方口径落库](../competition/official-brief-2026-11-06.md)
- 结论：**已导出并通过结构核验；同时澄清了「14 工具」与导出包「2 工具」的口径矛盾。**

## 1 产物

| 文件 | 内容 |
| --- | --- |
| `samples/nexent/live/urban_deduction_agent.zip` | 导出包 7 890 B，含 `agent.json` + 2 个技能 zip |
| `samples/nexent/live/agent_export.json` | 导出证据：HTTP 状态、字节数、整包 sha256、内容指纹、包内条目清单与逐项 sha256 |
| `deploy/nexent/export_bundle.py` | 导出 + 核验脚本（在 `nexent-config` 容器内跑，见第 6 节） |

包内结构：

```
urban_deduction_agent.zip
├── agent.json                              6 256 B
└── skills/
    ├── scarcity-allocation-evolution.zip   2 188 B
    └── urban-six-hop-deduction.zip         2 943 B
```

`agent.json` 顶层键：`agent_id`(=3) / `agent_info` / `mcp_info`。
`mcp_info` = `[{"mcp_server_name": "urban-twin-mcp", "mcp_url": "http://urban-twin-mcp:8000/sse"}]`。
Agent 本体：`urban_deduction_agent`（显示名「城市内涝跨部门决策智能体」）、`enabled=true`、模型 `DeepSeek-Chat`、`requested_output_tokens=8192`、`duty_prompt` 与 `constraint_prompt` 均在包内。

## 2 指纹：哪个能写进材料，哪个不能

**整包 sha256 不可复算**，实测连续导出 5 次得到 5 个不同的值（`2b7a89c9…` / `1819273a…` / `bce94c27…` / `b359b1e4…` / `c6da97af…`）。原因在平台侧：`export_agent_with_skills_impl` 用 `zipfile.ZipFile.writestr` 打包且未指定时间戳，zip 条目头带的是**导出当刻的本地时间**。

**内容指纹可复算**，5 次导出完全一致：

| 条目 | 字节 | sha256 |
| --- | --- | --- |
| `agent.json` | 6 256 | `8b0e084b0fbba936047f8b39bea7239ab3be33bbe01fb983963e9dd7213fcb08` |
| `skills/scarcity-allocation-evolution.zip` | 2 188 | `6aab9fab8cbb26a90b60e9f72aba1122f482ca955464e7ffdad8477b565993d5` |
| `skills/urban-six-hop-deduction.zip` | 2 943 | `b36d48792c3650c87ff7f306196044d27ea7c4c877b00abe5cc85ce7ddad6aa0` |
| **内容指纹**（上述三项按名排序拼接后再 sha256） | — | `adf49c002ee269a1d3f74d9410ebd89a0ca93121d968c47b56ac33c56202b6dd` |

材料写法：**写内容指纹 `adf49c00…`，并注明「由包内 `agent.json` 与 `skills/*.zip` 逐项 sha256 排序拼接计算」**。不要写整包 sha256——评委照做复算会对不上，反而像造假。

## 3 与平台技能仓库的交叉核对：逐位不一致，但语义一致

拿导出包内 `skills/*.zip` 里的 `SKILL.md` 与 `samples/nexent/live/skills.json`（上传时记录的本地文件哈希）比对，**sha256 全不相等**：

| 技能 | 导出包内 | 上传记录（`file_sha256`） | 字节 |
| --- | --- | --- | --- |
| `urban-six-hop-deduction` | `ba154970dadfc1f8` | `498a60abaf900ebe` | 包内 4 961 / 本地 4 966 |
| `scarcity-allocation-evolution` | `f01321a27bb73ec8` | `e7b090a4569cac37` | 差 5 B |

原因已定位：平台把 `SKILL.md` 的 YAML frontmatter 解析后用标准序列化回写——本地写的是块标量 `description: |`，导出包里变成单引号字符串 `description: '…'`。**用 `yaml.safe_load` 解析后逐字段比对，`name` / `tags` / `description` 全部相等**，正文部分字节一致。

材料写法：可说「导出包内技能与本地原文**语义一致**（frontmatter 逐字段相等、正文逐位一致）」，**不可说「逐位一致」**。

## 4 口径澄清：注册 14 个工具 ≠ Agent 挂 2 个

导出包 `agent.json` 的 `tools` 只有 2 项（`event_deduce`、`evidence_locate`，`source=mcp`），而台账写「14 个 MCP 工具注册到平台」。这两个数字都对，但指的是不同东西，**答辩时必须一句话讲清，否则评委打开导出包会当场追问**：

| 层级 | 数量 | 依据 |
| --- | --- | --- |
| 注册到平台 MCP 服务的工具 | **14** | `deploy/nexent/setup_nexent.py:76 OUR_TOOLS` |
| 主 Agent `urban_deduction_agent` 挂载 | **2**（`event_deduce`、`evidence_locate`） | `setup_nexent.py:89 MAIN_TOOLS`，导出包实测 |
| 协同 Agent `urban_collab_agent` 挂载 | **6**（含 `collab_run`） | `setup_nexent.py:131 COLLAB_TOOLS` |

主 Agent 收窄到 2 个是**有意的设计决策**，源码注释给了理由（`setup_nexent.py:83-88`）：模型真正读的「提示词」是工具列表，工具一多就会绕过 `event_deduce` 去调 `twin_neighbors` / `allocation_plan`，随后 `parallel_executor` 把回答截断在 `<|tool_call_start|>`，8 项结论一致性校验就守不住。

一句话说法：**「平台注册 14 个 MCP 工具，主 Agent 只拿 2 个结论类工具（收窄是稳定性设计），协同入口与其余工具挂在协同 Agent 上。」**

## 5 这暴露出的一个真实缺口

导出包只存 `mcp_info` 里的 **server 地址**，不存工具清单与工具实现。评委拿到 zip 复现时，若没有 `urban-twin-mcp` 服务端代码，这 14 个工具一个都起不来。

→ 初赛材料/复现包必须**同时交 MCP 服务端代码 + 启动说明**（`src/decision_poc/mcp_server.py` / `mcp_sse.py` + 容器启动方式），并说明「导出包恢复的是 Agent 配置，工具能力由 MCP 服务提供」。这一条已补进 [差距台账](../competition-gap-audit.md) 第 3 节。

## 6 复跑命令

```bash
docker cp deploy/nexent/platform_client.py nexent-config:/tmp/platform_client.py
docker cp deploy/nexent/export_bundle.py    nexent-config:/tmp/export_bundle.py
docker exec nexent-config sh -c "cd /opt/backend && .venv/bin/python /tmp/export_bundle.py \
    --agent-id 3 --out /tmp/urban_deduction_agent.zip --json /tmp/agent_export.json"
docker cp nexent-config:/tmp/urban_deduction_agent.zip samples/nexent/live/urban_deduction_agent.zip
docker cp nexent-config:/tmp/agent_export.json       samples/nexent/live/agent_export.json
```

前台校验：`agent_export.json` 里 `http_status=200`、`bundle.has_agent_json=true`、`bundle.skill_zips` 长度 2、`bundle.content_sha256=adf49c00…`（Agent 或技能内容改动后此值会变，属预期）。
