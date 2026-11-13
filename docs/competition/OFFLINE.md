# 离线部署说明书（OFFLINE.md）

- 适用：赛题 3 硬要求「作品须支持离线部署包，不能强依赖在线环境」`[官方赋能直播 14:38]`
- 性质：**可执行文档**。本文每个命令都在本机实测过（2026-11-11，Nexent v2.7.0 / 15 容器 / `/nb/v1/health` healthy）。
  体积与裁剪结论引自 [离线部署评估](offline-deploy-assessment.md)，未实测的部分明确标注「待验证」。
- 打包位置约定：构建完成后本文应置于离线包**根目录**，与 `load-images.sh`、`deploy.sh` 同级。

---

## 1 资源要求

| 项 | 官方口径 | 本机实测 |
| --- | --- | --- |
| CPU | 4 核最低 / 8 核推荐 `[直播 10:40]` | 未记录 |
| 内存 | **8 GiB 最低 / 16 GiB 推荐** | 7.79 GiB（`MemTotal=8327397376`），贴边可用 |
| 磁盘 | 40 GiB 最低 / 100 GiB 推荐 | 镜像解压后约 17.5 GiB，另需卷数据空间 |
| 软件 | Docker 24+ / Compose v2+；Windows 需 Docker Desktop + WSL2 | Docker 29.9.1 + Docker Desktop，overlayfs |

**内存是本项目最紧的一项**：`nexent-data-process` 常驻约 4.3 GiB，全栈稳态占用约 6.3/7.75 GiB（≈80%），
一跑推理就接近上限。**建议把 Docker Desktop 内存上限设到 12 GiB** 再压测，否则失败会被误判成"平台不通"。

## 2 包结构（官方打包器产出）

```
nexent-offline/
├── images/                 # 14 个镜像 tar（docker save）
├── load-images.sh          # 批量 docker load
├── deploy.sh / uninstall.sh
├── push-images.sh          # 推内网仓库时用
├── deploy/                 # 部署脚本、compose、SQL
├── manifest.yaml
├── checksums.txt           # 逐文件校验
└── OFFLINE.md              # 本文
```

镜像清单（`images/`，**14 个 tar / 整包 4.8 GB**，2026-11-11 实测）：

```
nexent/nexent:v2.7.0                     nexent/nexent-web:v2.7.0
nexent/nexent-mcp:v2.7.0                 nexent/nexent-data-process:v2.7.0
nexent/nexent-sandbox:v2.7.0
urban-twin-mcp:dev                       ← 本项目的 MCP 服务，必需（打包器不认识，需单独 save）
docker.elastic.co/elasticsearch/elasticsearch:8.17.4
postgres:15-alpine                       redis:alpine
quay.io/minio/minio:RELEASE.2023-12-20T01-00-02Z
kong:2.8.1                               supabase/gotrue:v2.170.0
supabase/postgres:15.8.1.060
otel/opentelemetry-collector-contrib:0.151.0
```

> **两条必须记住的补齐项**（官方打包器不会自动带上，漏了断网必失败）：
> ① `urban-twin-mcp:dev` 是本地构建标签，**不在任何公共仓库**；缺了它一个 MCP 工具都起不来。
> ② `otel/opentelemetry-collector-contrib` 由 `docker-compose-monitoring.yml` 引用，**官方 manifest 里没有它**；
>    断网时 pull 失败 → 监控栈起不来 → 连带 config/runtime 起不来（症状见 6.1）。
> 补齐方式：`docker save <image> -o images/<name>.tar`，再登记进 `manifest.yaml` 的 `images:`，最后重算 `checksums.txt`。
>
> 不需要的：`kg-mcp-server:0.1`（Nexent 开发态知识图谱 MCP，compose 未引用，本项目也不使用）。

> `urban-twin-mcp:dev` 是本地构建标签，**不在任何公共仓库**。评委拿到包后若缺这个 tar，一个 MCP 工具都起不来。

## 3 构建（在有网络的机器上）

优先用官方打包器，不要手搓 compose：

```bash
cd /path/to/Nexent
bash deploy/offline/build_offline_package.sh \
     --target docker \
     --compress false \
     --defaults \
     --output-dir "D:/华为ICT/dist-offline/nexent-offline"
```

参数说明：`--target docker`（k8s 用不到）、`--compress false`（先出目录便于校对，分发前再压 zip）、
`--defaults`（跳过交互，复用 `deploy/deploy.options` 或内置默认）、`--output-dir` 指定输出。

**构建前置条件**：本机镜像必须与 `deploy/env/image-source.*.env` 里的 tag 完全一致，否则打包器会去拉网。
核对命令：`docker images --format "{{.Repository}}:{{.Tag}}"`。

## 4 安装（在离线机器上，三步）

```bash
# 1. 灌镜像（脚本内部按 images/ 顺序 docker load）
bash load-images.sh

# 2. 校验（可选但建议：逐文件 sha256）
sha256sum -c checksums.txt

# 3. 部署（复用官方部署链路，不要自己 compose up）
bash deploy.sh --defaults --load-images docker
```

若目标机已配好 `deploy/env/.env`，脚本会原样保留；否则从 `deploy/env/.env.example` 生成。
**部署成功后立刻把 `deploy/env/.env` 冻结**——它是复现的第一步，也是排障的基准。

## 5 验收（五条，逐条可判定）

| # | 检查 | 期望 |
| --- | --- | --- |
| 1 | `docker ps --format "{{.Names}}\t{{.Status}}"` | 15 个容器 Up；`nexent-elasticsearch` / `nexent-redis` / `supabase-*-mini` healthy |
| 2 | `curl -s http://127.0.0.1:5013/nb/v1/health` | `{"status":"healthy","service":"northbound-api"}` |
| 3 | `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:3000/zh` | `200` |
| 4 | 无密钥访问 `curl -s -o /dev/null -w "%{http_code}" http://127.0.0.1:5013/nb/v1/agents` | `401`（鉴权边界生效） |
| 5 | 浏览器登录 → 建/开 Agent → 跑一次六跳对话 | 结论与离线 8/8 一致 |

端口（本机实测）：Web `3000`、北向 `5013`、config `5010`、runtime `5014`、MinIO `9010/9011`、Redis `6379`、
MCP 服务 `18000→8000`。其余端口以 `docker ps --format "{{.Names}}\t{{.Ports}}"` 现场输出为准。

## 6 故障排查（三条，都是实测踩过的）

### 6.1 登录报「认证服务当前不可用」——其实是后端在崩溃循环

**症状**：前端登录页提示认证服务不可用；`supabase-auth-mini` 显示 healthy，容易被误判成认证故障。

**根因**：`deploy/env/monitoring.env` 里 `OTEL_EXPORTER_OTLP_PROTOCOL="http"`，而 FastAPI 自动埋点只接受
`http/protobuf`。`nexent-config` 与 `nexent-runtime` 启动即 `Application startup failed. Exiting.`，
`restart: always` 让它们每 20 秒死一次，`docker ps` 里仍显示 `Up`。
前端 `HTTP_BACKEND=http://nexent-config:5010`，config 死了就统称"认证不可用"。

**修法**：改为 `OTEL_EXPORTER_OTLP_PROTOCOL="http/protobuf"`，然后**必须重建容器**：

```bash
cd deploy/docker/compose
docker compose -p nexent -f docker-compose.prod.yml --env-file ../.env.generated \
    up -d --force-recreate nexent-config nexent-runtime
```

`docker restart` **不生效**——env 在容器创建时就固化了。

### 6.2 `--force-recreate` 会连带重建依赖，把 ES 搞成 unhealthy

**症状**：只重建 runtime，结果 `nexent-elasticsearch` 一起被重建成 unhealthy，runtime 因依赖失败起不来。

**根因**：compose 会连同依赖一起重建；而 `--env-file ../.env.generated` 里**没有 `ELASTIC_PASSWORD`**
（`deploy/env/.env` 里才有），ES 拿到空密码直接起不来。

**修法**：先 `source` 再 compose，让变量进 shell 环境：

```bash
set -a && source deploy/env/.env && set +a
cd deploy/docker/compose && docker compose -p nexent -f docker-compose.prod.yml \
    --env-file ../.env.generated up -d --force-recreate nexent-elasticsearch nexent-runtime
```

### 6.3 超级管理员登录 401——`.env` 里的密码和库里不一致

**症状**：用 `NEXENT_SUPER_ADMIN_PASSWORD` 登录返回 `401 Email or password error`。

**根因**：`suadmin@nexent.com` 是更早一次部署创建的，`deploy.sh` 检测到已存在就跳过创建，
之后改 `.env` 不会同步到认证库。

**修法**（注意库名是 `supabase`，不是 `postgres`；pgcrypto 在 `extensions` schema）：

```bash
docker exec -i supabase-db-mini psql -U postgres -d supabase -c \
"update auth.users set encrypted_password = extensions.crypt('<新密码>', extensions.gen_salt('bf')) where email='suadmin@nexent.com';"
```

另有 `admin@nexent.com`（镜像自带，口令未知），演示统一用 `suadmin@nexent.com`。

## 7 已知边界（材料里要写明，别等评委问）

1. **离线部署 ≠ 离线推理**。平台与工具链可完全离线，但 LLM 必须有一个可达的 OpenAI 兼容端点。
   真断网环境下需把模型指向**内部部署的推理服务**（vLLM / Ollama 等，暴露 `/v1` 接口）；
   否则对话能开、无模型可用。这一条必须在 PPT 与 OFFLINE 里都写清，不能含糊。
2. **不裁 sandbox 是有依据的**：`setup_nexent.py` 的 `DUTY_PROMPT` 已禁用 `python_interpreter` 与
   `parallel_executor`，全链路不执行沙箱代码，理论可省 1.89 GB；但「停掉 sandbox 后 8/8 是否仍通」**待真机验证**，
   未验证前不从包里删。
3. **`doc_search` 与主 Agent 的关系**：主 Agent 挂三个工具（`event_deduce` / `evidence_locate` /
   `doc_search`）是 `MAIN_TOOLS` **有意收窄**的结果——15 个工具里只留这三个。2026-11-13 才加进
   `doc_search`，因为真实预案进库后推演需要回答"依据哪一条"；但它的用途被 `DUTY_PROMPT` 限死：
   **只查规则依据，不得替代 `event_deduce` 提供事实**，且最多调一次。加完重跑八项一致性校验仍是 8/8。
   评委问"为什么 15 个工具只挂这三个"时，这是设计取舍，不是缺陷。
4. **导出包不含 MCP 实现**：平台导出的 `agent.json` 只存 MCP 的 server 地址，工具能力由 `urban-twin-mcp` 提供，
   所以离线包必须同时带 `urban-twin-mcp:dev` 镜像与 `src/decision_poc/mcp_server.py`。

## 8 复现凭据

- 构建产物校验：`checksums.txt`（逐文件 sha256）；整包 sha256 不稳定（zip 头含时间戳，实测 5 次 5 个值），
  以文件级校验为准；
- 本机基线：15 容器 / `/nb/v1/health` healthy / `:3000/zh` 200 / `pytest` 977 collected / `evidence_check` 108/108。
