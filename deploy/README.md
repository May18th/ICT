# deploy/ — 三条部署路径

同一个决策服务有三种落地方式，按"离评委多远"分开，互不替代：

| 路径 | 目录 | 干什么 | 需要什么 |
|---|---|---|---|
| 本地 MCP 容器 | `mcp_sse/` + `docker-compose.mcp.yml` | 把 MCP 服务打成容器，验证 stdio / SSE 两种传输 | Docker |
| 平台装配 | `nexent/` | 把工具、技能、智能体装到 Nexent 平台，并抓真机证据 | Nexent 平台 + 凭据 |
| 离线交付 | `offline/` | 无网环境用镜像 tar + 校验清单部署 | 预置的镜像包 |

启动本地 MCP：

```powershell
.\up_mcp.ps1
```

## 平台装配

见 [`nexent/README.md`](nexent/README.md)。脚本按职责分四类：

- **注册**：模型 provider、MCP 工具、智能体（`register_*.py`、`setup_nexent.py`）
- **装配**：技能绑定、知识入库、Agent 内容填充（`bind_skills.py`、`ingest_knowledge.py`）
- **取证**：平台界面截图、真机调用抓取、版本快照（`capture_*.py`、`snapshot_platform_versions.py`）
- **核对**：台账同步、token 清理、草稿去重（`check_ledger_sync.py`、`prune_tokens.py`）

**这些脚本打真实平台，要凭据与网络，不进 CI。** CI 只跑 `src/` 与 `tests/`。

## 离线交付

见 [`offline/OFFLINE.md`](offline/OFFLINE.md)。

`make_manifest.py` 对每个镜像 tar 算 sha256，写进 `images/SHA256SUMS`；`install-offline.sh` 负责 load → 校验 → 拉起 → 健康检查。

**交付状态以 OFFLINE.md 为准，不在别处复述。** 那边的规矩是"没做就写没做"——镜像包产出与断网实测是两件事，别混成一个"已验证"。
