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

## 实际执行结果（2026-11-21 20:54）

已录得 44 秒界面演示，存 `_archive_deliverables_20261121/screen-recording/nexent_platform_demo_20261121.mp4`（1.1MB，2560×1440 / 15fps）。

| 实际段落 | 内容 |
|---|---|
| 第 1 段 | MCP `/health` 的 JSON，工具列表与 `tools_count` |
| 第 2 段 | 平台「选择智能体」弹窗，六个 Agent 与版本号 v34 / v12 / v11 |
| 第 3 段 | 对话配置里的知识库索引，含 `urban-policy-ledger`（2 篇文档） |
| 第 6 段 | 门户 `/evolution`：0.2264→0.0000 等指标与 V1/V2 对照 |

**第 3、4 段（主推演与协同问答）没有录**：平台前端渲染回答时抛 `Minified React error #185`（Maximum update depth exceeded），回答区域变成红框报错。主推演、协同、以及把问题缩到 250 字不输出 JSON 都复现，所以不是内容太长；后端同事件调用正常（8/8）。问答证据继续用 `samples/nexent/live/` 下的 JSON。

录制方式：`ffmpeg -f gdigrab -framerate 15 -i desktop`，OBS 这条路不可用——本机 OBS 33.0.1 的 GUI 进程启动后会被环境回收，websocket 4455 随之消失。

