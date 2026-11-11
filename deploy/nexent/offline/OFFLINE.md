# Nexent 离线部署说明（urban-twin 决策智能体）

- 版本：Nexent v2.7.0 + `urban-twin-mcp:dev`
- 适用：评审/复现环境**无外网**，或不允许在线拉镜像的机器
- 实测环境：Windows 11 + Docker Desktop（WSL2 后端），2026-11-11
- 本包解决的问题：官方要求「作品不能强依赖在线环境，须支持离线部署包」

---

## 1 包结构

```
offline-package/
├── images/                 # 14 个镜像 tar（合计 4.6 GB）+ SHA256SUMS
├── nexent/                 # Nexent v2.7.0 源码（含 deploy/ 与 sql/）
├── scripts/
│   ├── load-images.sh      # 校验 + 加载镜像
│   ├── install-offline.sh  # 部署 + 等待就绪
│   └── verify-offline.sh   # 自检（镜像/容器/端点）
├── image-manifest.txt      # 镜像清单与角色说明
└── OFFLINE.md              # 本文件
```

> **不要把 `images/` 放进 Nexent 源码目录**。`nexent/deploy/` 下的部署脚本会扫描自身目录，
> 4.6 GB 的 tar 混进去会让若干脚本变慢，且容易误提交。

## 2 环境要求

| 项 | 最低 | 推荐 | 说明 |
|---|---|---|---|
| CPU | 4 核 | 8 核 | 官方口径：做数据处理至少 4 核 |
| 内存 | 8 GB | **16 GB** | 实测 7.9 GB 可跑，但 `nexent-data-process` 常驻 4.3 GB，并发推理易 OOM |
| 磁盘 | 25 GB | 40 GB | 镜像解压后约 13–15 GB，另加日志与卷 |
| Docker | 24+，Compose v2 | 同 | Windows 需 Docker Desktop + WSL2 |
| 网络 | **不需要** | — | 安装完成后全程可断网 |

## 3 三步安装

```bash
# ① 加载镜像（约 2–5 分钟，含 4.6 GB 校验）
./scripts/load-images.sh

# ② 部署（首次 4–8 分钟）
#    若 Nexent 源码不在 ./nexent，用 NEXENT_DIR=/path/to/nexent 指定
./scripts/install-offline.sh

# ③ 自检（可反复跑）
./scripts/verify-offline.sh
```

全部通过后访问 `http://localhost:3000`。
管理员账号 `suadmin@nexent.com`，口令见 `nexent/deploy/env/.env` 的 `NEXENT_SUPER_ADMIN_PASSWORD`。

### 3.1 启动 MCP 服务（决策工具的实际执行者）

Nexent 里注册的 MCP 工具只是「地址」，真正的实现跑在 `urban-twin-mcp` 容器里。
`deploy.sh` 不会启动它，需要单独一条命令：

```bash
docker run -d --name urban-twin-mcp --restart unless-stopped \
  --network nexent_network \
  -e PYTHONPATH=/app/src -e PYTHONUNBUFFERED=1 \
  -p 127.0.0.1:18000:8000 \
  urban-twin-mcp:dev \
  python -m decision_poc.mcp_sse --host 0.0.0.0 --port 8000
```

验证：`curl http://127.0.0.1:18000/health` 返回 200。
然后在 Nexent「MCP 配置」里确认服务地址为 `http://urban-twin-mcp:8000/sse`（容器网络内）。

> **导出包不包含工具实现**：`<agent>.zip` 只存 `mcp_info` 的地址，不含工具清单与代码。
> 换机器复现时必须同时起这个 MCP 容器，否则 Agent 一个工具都调不到。

## 4 端口占用

| 端口 | 服务 |
|---|---|
| 3000 | Web 前端 |
| 5010 / 5014 / 5013 | config / runtime / 北向 API |
| 9010 / 9011 | MinIO API / Console |
| 18000 | urban-twin-mcp（宿主侧，仅 127.0.0.1） |
| 6379 / 5432 / 9200 | Redis / PostgreSQL / Elasticsearch |

与本项目离线可视化 `web_demo`（8787）不冲突。

## 5 故障排查（实测踩到的三个坑）

### 5.1 登录报「认证服务当前不可用」

**先别看认证服务。** Web 的 `HTTP_BACKEND=http://nexent-config:5010`，config 没起来时前端统称「认证不可用」。

```bash
docker logs --tail 20 nexent-config | grep -E "Uvicorn running|startup failed"
```

若看到 `FastAPI automatic telemetry requires the OTLP http/protobuf protocol`，就是下面这条。

### 5.2 config / runtime 启动即退出（OTEL 协议非法）

- **症状**：容器 `Up` 但每 20 秒死一次；日志末尾 `Application startup failed. Exiting.`
- **根因**：`nexent/deploy/env/monitoring.env` 里 `OTEL_EXPORTER_OTLP_PROTOCOL="http"`，
  FastAPI 自动埋点只接受 `http/protobuf`
- **修法**：改成 `http/protobuf`，然后**必须重建容器**：

```bash
cd nexent && set -a && source deploy/env/.env && set +a
cd deploy/docker/compose
docker compose -p nexent -f docker-compose.prod.yml --env-file ../.env.generated \
  up -d --force-recreate nexent-config nexent-runtime
```

`install-offline.sh` 已内置这条自动修正。**注意 `docker restart` 无效**——env 在容器创建时固化。

### 5.3 重建服务时把 Elasticsearch 一起搞挂

- **症状**：`docker compose up -d --force-recreate nexent-runtime` 之后 ES 变 unhealthy，runtime 因依赖失败起不来
- **根因**：compose 会连带重建依赖；而 `--env-file .env.generated` 里**没有 `ELASTIC_PASSWORD`**
  （正常部署靠 `source deploy/env/.env` 的 shell export 传值），ES 拿到空密码
- **修法**：永远先 `set -a && source deploy/env/.env && set +a` 再执行 compose；
  或直接重跑 `bash deploy.sh --defaults docker` 让它自己渲染

### 5.4 管理员口令与库内不一致

- **症状**：`.env` 里的口令登录返回 401 `Email or password error`，但账号确实存在
- **根因**：`deploy.sh` 发现 `suadmin@nexent.com` 已存在就跳过创建，改 `.env` 不会同步到库
- **修法**（库名是 `supabase`，不是 `postgres`；pgcrypto 在 `extensions` schema）：

```bash
docker exec -i supabase-db-mini psql -U postgres -d supabase -c \
"update auth.users set encrypted_password = extensions.crypt('<新口令>', extensions.gen_salt('bf'))
 where email='suadmin@nexent.com';"
```

### 5.5 `sha256sum -c` 报 `No such file or directory` 但文件明明在

- **根因**：`SHA256SUMS` 是 CRLF 换行，文件名后面带了 `\r`
- **修法**：用 LF 重写该文件（本包已修，2026-11-11 重新生成，14/14 校验通过）

## 6 断网验证怎么做才算数

1. 拔网线 / 关闭 WLAN / `docker network disconnect`（三选一，拔网线最彻底）；
2. `docker compose down` 后重新 `./scripts/install-offline.sh`；
3. 若某个镜像缺失，compose 会因 `pull access denied` 直接失败，**不会静默降级**——这本身就是离线证明；
4. 跑一次六跳对话，记录 `conversation_id`，与离线 8/8 的结论比对。

## 7 已知边界（如实声明）

- 本包**不含** LLM 权重与 API Key。模型需在 `http://localhost:3000/zh/models` 里自行登记
  （内置目录已预置 DeepSeek，填自己的 key 即可）。**Nexent 不读宿主机环境变量里的 key。**
- `nexent-data-process`（6.77 GB）保留未裁：初赛材料若被问「知识库能力」，不能答「我们没用」。
  沙箱镜像（1.89 GB）同样保留——虽然我们的提示词禁用了 `python_interpreter`，
  但裁剪需真机验证，未验证前不做，避免演示时缺能力。
- 首次启动后 Memory 需 16 GB 才稳；8 GB 环境建议关闭 `nexent-data-process` 之外的额外负载再压测。
