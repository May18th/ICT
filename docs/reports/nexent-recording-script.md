# Nexent 平台问答录屏拍摄清单

用途：补上「当前 v34 的问答全过程」这一项目前唯一缺的外部证据。录屏只证明操作流程与界面表现，**不替代**北向接口、日志与快照——多事件隔离、7 字段契约、指纹一致性仍以 `samples/nexent/live/` 下的 JSON 为准。

## 录制前检查

```bash
docker ps --filter name=urban-twin-mcp            # 必须 healthy
curl -s http://127.0.0.1:18000/health             # tools_count = 15
curl -s -o /dev/null -w "%{http_code}\n" http://localhost:5013/nb/v1/agents
```

三项都正常再开录。平台登录用本机管理员账号（凭据只走环境变量，不写进仓库和画面）；**录屏时不要打开 `.env`、不要在终端回显 `NEXENT_API_KEY`、不要切到容器 `env` 输出**。

## 分镜

| 段 | 操作 | 画面要点 | 目标时长 |
|---|---|---|---|
| 1 环境 | 终端执行上面三条命令 | 容器 healthy、`tools_count: 15`、北向 200 | 20s |
| 2 Agent 列表 | 平台「智能体」页 | 6 个 Agent，`urban_deduction_agent` v34、`urban_collab_agent` v12、`urban_asset_qa` v11 | 20s |
| 3 主推演 | 对主 Agent 提 `WO-20261029-007`，时点 `2026-10-29T09:40:00+08:00` | 工具调用展开可见 `event_deduce`；答案含六跳、三类冲突、资源缺口 2 台 | 60s |
| 4 协同 | 对 `urban_collab_agent` 提同一事件 | `collab_run` 调用；答案里 `kernel_fingerprint` 为 `d7fa402fe22e596c`，卡片 26 张 | 60s |
| 5 台账 | 「知识库」页打开 `urban-policy-ledger` | 两篇文档：`policy-ledger://flood-response-v1.2`、`platform-versions://snapshot`，状态 COMPLETED | 20s |
| 6 对照 | 打开 `/evolution` 页 | 台账状态显示「已覆盖本地最新反馈」 | 15s |

第 6 段的作用是把「台账不是落后一轮」拍进画面，对应 `artifacts/ledger/ledger_sync.json` 的 `stale=false`。

## 收尾

- 录制文件存到工作区外（与交付材料同策略，不进 git）。
- 在 `docs/reports/nexent-evidence-index.md` 的「尚未取得的外部证据」中改写该条，并附文件路径与录制时间。
- 若只做静态材料，可先跑 `python deploy/nexent/capture_platform_ui.py` 出三张界面截图，但截图不能替代第 3、4 段的交互过程。
