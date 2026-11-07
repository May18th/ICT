# OFFLINE.md — 离线部署指南（初赛交付物）

状态：**镜像包已产出，断网实测待执行**（见 §7，不装样子，没做就写没做）。

## 1 包内容

```
dist-offline/
├── images/*.tar          # 14 个镜像（逐个 save，文件名即 镜像名_标签.tar）
├── images/SHA256SUMS     # 每个 tar 的 sha256（由 make_manifest.py 生成）
└── image-list.txt        # 权威镜像清单
agent_poc/deploy/offline/
├── install-offline.sh    # load → 校验 → 提示拉起 → 健康检查
├── make_manifest.py      # 重新生成校验清单
└── OFFLINE.md            # 本文
```

另需拷贝两个代码仓到离线机（compose、env、种子数据都在仓里）：`Nexent/` 与 `agent_poc/`。

## 2 资源需求

- 官方口径：数据处理场景至少 **4 核 16G**，内存不足会导致文件处理失败 `[直播 10:26]`
- 本机实测参照：i7 + RTX 4060 Laptop，17 容器连续运行 4 小时无重启，`supabase-db-mini` / `urban-twin-mcp` / `elasticsearch` 均 healthy
- 磁盘：tar 合计约 17.5 GB，load 后占用相当

## 3 端口占用（宿主机侧）

| 端口 | 服务 |
| --- | --- |
| 3000 | nexent-web（唯一对外入口，307 → /zh） |
| 5010 / 5013 / 5014 / 5015 | config / northbound / runtime / mcp 管理 API |
| 18000 | urban-twin-mcp（`/health` 返回 tools_count=15） |
| 5432 / 6379 / 9200 / 9000 | 平台内部库（可只绑内部网络） |

Windows 注意：Hyper-V 会保留一批端口，实测 **8787/8788 被保留（WinError 10013）**，换 18901 可用；`netsh interface ipv4 show excludedportrange protocol=tcp` 可查。

## 4 安装步骤

```bash
# 1) 装好 Docker，核对镜像齐整
bash agent_poc/deploy/offline/install-offline.sh   # 只做 load + 校验

# 2) 拉起 Nexent 全栈 —— registry 必须选 general！
#    tar 里的镜像名与 image-source.general.env 一一对应；
#    选 mainland 会引用 ccr.ccs.tencentyun.com/* 镜像，离线包里没有，compose 会去拉然后超时。
bash Nexent/deploy/docker/deploy.sh   # 交互选 general

# 3) 拉起 urban-twin-mcp（agent_poc 侧 compose）
docker compose -f agent_poc/deploy/urban-twin-mcp/compose.yaml up -d   # 以仓内实际路径为准

# 4) 初始化平台内容（一次性）
#    setup_nexent.py：建 Agent / 传技能 / 绑 MCP / mint-key，步骤与在线部署完全一致
python -m deploy.nexent.setup_nexent   # 以仓内实际入口为准，见 agent_poc/deploy/nexent/

# 5) 健康检查
curl -s -o /dev/null -w "web=%{http_code}\n" http://localhost:3000/zh   # 200
curl -s http://localhost:18000/health                                    # tools_count=15
python -m decision_poc.nexent_urban                                      # 8/8
```

## 5 常见失败（都真实踩过）

| 症状 | 原因 | 处置 |
| --- | --- | --- |
| compose 拉镜像超时 | registry 选了 mainland，引用 ccr 云端镜像 | 重跑 deploy.sh 选 general |
| 端口 bind 报 WinError 10013 | Hyper-V 保留端口 | 换端口，见 §3 |
| web 起来但标题是 "AI Agent Platform" | 用了官方 `nexent-web:v2.7.0` 而非定制 tag | 确认 compose 用 `v2.7.0-ict`（prod.yml 已写死） |
| 注册接口报 INVITE_CODE_INVALID | 邀请码与 `.env` 不一致 | `.env` 的 `INVITE_CODE` 为部署时配置值 |
| nexent-config 反复重启 | PostgreSQL 未就绪（冷启动竞态） | 等 PG healthy 后自动恢复，不要急着重装 |
| 重建容器后 env 未更新 | `docker restart` 不重读 env_file | 必须重建容器（compose up --force-recreate） |

## 6 已知边界

- 不含 `nexent/nexent-mcp:v2.7.0`（平台内置 MCP 容器镜像）：当前架构 MCP 走 `urban-twin-mcp:dev`，未使用内置容器方案。
- 不含 terminal 工具镜像：`ENABLE_TERMINAL_TOOL_CONTAINER=false`，全链路禁用沙箱执行。
- 模型不走本地推理：Agent 调用外部 LLM API，**离线机上仍需能访问所选模型服务**（按官方离线口径理解为"不依赖在线开发环境"，模型侧网络要求见现场网络方案）。若现场完全断网，需另备本地推理方案（当前未配置）。

## 7 断网实测（步骤已备好，待执行）

**当前状态：未执行。** 执行后在本节补：日期、断网方式、通过项、失败项。在此之前，本包的对外表述是「已产出离线镜像包，断网从零重建实测待补」——不得写成「已验证」。

前置确认（2026-11-07 已做）：14/14 个 tar 结构校验通过（`tar -tf` + `manifest.json` 可读）。

### 档 1：离线指令链路验证（低风险，服务中断约 5 分钟，可随时做）

验证 compose 全程不碰网、load 指令可用。不动镜像与卷，只 down/up。

```bash
# 1) 停全栈（保留镜像与卷）
docker compose -p nexent -f <nexent的compose>.yml down
docker compose -f <urban-twin-mcp的compose>.yml down

# 2) 断网（关 WLAN / 拔网线），ping 一个外网 IP 确认断开

# 3) 重放 load（镜像已存在，验证命令与 tar 可读性）
for tar in dist-offline/images/*.tar; do docker load -i "$tar"; done

# 4) 离线拉起（--pull never 强制禁止在线拉取，等效断网检查）
docker compose ... up -d --pull never

# 5) 健康检查（§4 第 5 步）+ 跑一次六跳对话

# 6) 记录：起止时间、每步结果、失败与处置
```

### 档 2：真从零重建（高风险，只建议在备份机 / 大版本节点做）

```bash
docker ps -aq | xargs docker rm -f        # 清全部容器（卷保留）
docker images -q | xargs docker rmi -f    # 清全部镜像（不可逆，确认 tar 14/14 校验过再执行）
# 断网 → install-offline.sh → compose up → 健康检查 → 六跳对话 → 截图留证
```

风险提示：删除镜像前必须再次确认 `SHA256SUMS` 与 tar 在本机；卷一旦删除，台账/会话数据不可恢复，**档 2 永远不要动卷**。

### 判定标准（两档通用）

通过 = ① load 全部成功无报错；② compose up 全程无 pull 尝试（日志无 "Pulling"）；③ 15 容器全 Up 且 healthy；④ `/health` tools_count=15；⑤ 六跳对话 8/8。任一不满足即失败，记录现场后回退（恢复网络按原部署文档重建）。

## 附录 A：镜像清单（权威，14 个，共 4.92 GB）

与 `dist-offline/image-list.txt` / `images/SHA256SUMS` 同源。save 保留镜像层原始压缩格式，故 tar 体积（4.92 GB）远小于 `docker images` 显示的解压体积（17.56 GB）。

| 镜像 | 角色 | tar 体积 |
| --- | --- | --- |
| `nexent/nexent:v2.7.0` | 后端主服务（config/runtime/northbound/mcp 四服务共用） | 418 MB |
| `nexent/nexent-web:v2.7.0-ict` | 前端（**ICT 定制版**，站名/标题已改） | 133 MB |
| `nexent/nexent-web:v2.7.0` | 前端（官方版，回滚备用） | 133 MB |
| `nexent/nexent-data-process:v2.7.0` | 文档处理/知识库切分 | 1.93 GB |
| `nexent/nexent-sandbox:v2.7.0` | 代码沙箱（全链路禁用执行，保留备用） | 412 MB |
| `urban-twin-mcp:dev` | **本项目 MCP 服务**（15 工具） | 72 MB |
| `docker.elastic.co/elasticsearch/elasticsearch:8.17.4` | 全文检索 | 711 MB |
| `postgres:15-alpine` | MCP 侧库 | 116 MB |
| `redis:alpine` | 缓存 | 40 MB |
| `quay.io/minio/minio:RELEASE.2023-12-20T01-00-02Z` | 对象存储 | 100 MB |
| `kong:2.8.1` | supabase 网关 | 49 MB |
| `supabase/gotrue:v2.170.0` | 认证 | 23 MB |
| `supabase/postgres:15.8.1.060` | 平台库 | 683 MB |
| `otel/opentelemetry-collector-contrib:0.151.0` | 链路追踪 | 99 MB |

镜像名与 `Nexent/deploy/env/image-source.general.env` 一一对应 —— **离线部署 registry 必须选 general**。
