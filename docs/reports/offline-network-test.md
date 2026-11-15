# 断网能力实测：哪些能离线跑，哪些不能

> 结论先说：**决策核心全链路离线实测通过**（六步、分配、15 个 MCP 工具、检索），输出与联网时逐字一致。
> **平台 Agent 编排层离线不可用**——它要调 4 个公网模型端点。这是配置事实，不是没测。
> 整个容器栈的"冷启动"未测（会拆掉当前演示栈），见第 5 节。

测试时间：2026-11-14　测试机：本机（Windows + Docker 29.9.1）
方法：`docker run --network none` / `--network <internal>` 把被测进程放进**无路由、无 DNS** 的网络里，
不是"拔网线猜"，而是可复现、可反重跑的网络隔离。

---

## 1 网络隔离是真的隔离

```
$ docker network create --internal offline-sim
$ docker run --rm --network offline-sim urban-twin-mcp:dev python -c "socket.create_connection(('api.deepseek.com',443))"
api.deepseek.com      BLOCKED gaierror
api.modelarts-maas.com BLOCKED gaierror
```

对照组（默认网络）同一个探测：`container egress: OK`。
所以下面所有"通过"都是在**确实没有外网**的条件下取得的。

---

## 2 决策核心：离线通过

| 项 | 命令 | 结果 |
|---|---|---|
| 六步推演 | `python -m decision_poc.deduction --event WO-20261029-007 --as-of 2026-10-29T09:40:00+08:00` | 通过，与联网输出**逐字一致**（`diff --strip-trailing-cr` 无差异，仅 Windows CRLF 不同） |
| MCP 工具清单 | `UrbanToolkit().tools()` | **15 个**全部注册：semantic_query / twin_neighbors / doc_search / evidence_locate / claim_trace / provenance_verify / event_deduce / counterfactual_run / conflict_check / allocation_plan / scene_render / scene_status / scene_compare / asset_lookup / collab_run |
| `event_deduce` | 带 `caller_labels=['ORG-EMERG']` | `status=ok`，返回 steps / conflicts / resource_gaps / residual_risks |
| `allocation_plan` | 同上 | `status=ok`，返回 policy + allocation + snapshot_hash |
| `doc_search`（检索） | `query=橙色预警 封路阈值` | `status=ok`，命中 2 条，**走本地索引，不调外部 embedding** |

一个副作用发现（是设计，不是缺陷）：不带 `caller_labels` 调 `event_deduce` 会返回

```
{"status":"error","error":{"code":"PERMISSION_DENIED","message":"推演引用了对当前调用方不可见的依据，不能只给结论不给依据"}}
```

即"证据不可见就不给结论"。这条在说明里可以当加分项讲，但演示时必须传 `caller_labels`，否则会看到 error 以为坏了。

**边界**：`python -m decision_poc.evidence_check`（108 项自检）**在部署镜像里跑不了**——
它要读 `README.md` 与 `docs/`，而运行镜像只带 `src/ + samples/ + artifacts/`。
它不是运行时组件，属于仓库内的开发期自检，这个分工是合理的，但别说"部署包里也能自检"。

---

## 3 离线包：镜像齐备，不需要 pull

| 检查 | 结果 |
|---|---|
| `manifest.yaml` 列出的镜像 | 14 个 |
| 本机已存在 | **14 / 14，缺失无** |
| 包体积 | 4.8 GB（`images/*.tar` + 源码 + `OFFLINE.md`） |
| 文件校验 | 3391 个文件，0 失败（2026-11-11 构建时记录） |

所以"断网时因 pull 失败起不来"这个失败模式**不存在**——镜像全在本地，`docker load` 即可。

---

## 4 平台编排层：离线不可用（4 个端点全在公网）

平台侧模型登记表（`nexent.model_record_t`，实查）：

| 模型 | 用途 | 端点 |
|---|---|---|
| deepseek-chat | LLM | `https://api.deepseek.com/v1` |
| openpangu-2.0-pro | LLM | `https://api.modelarts-maas.com/v1` |
| bge-m3 | embedding | `https://api.modelarts-maas.com/api.../embeddings` |
| bge-reranker-v2-m3 | rerank | `https://api.modelarts-maas.com/v1/rerank` |

**四个全是公网 HTTPS。** 断网后容器能起、页面能开，但 Agent 一次模型调用都发不出去，
表现为"对话能开、无模型可用"。这一条 `dist-offline/nexent-offline/OFFLINE.md` 第 164 行已经写了，
本次实测把它从"文档提醒"变成"已确认"。

**这意味着"离线部署"目前只到第 2 级，不到第 3 级：**

| 级别 | 含义 | 现状 |
|---|---|---|
| L1 | 容器能起、页面能开 | 具备（镜像齐备） |
| L2 | 决策核心能出结论 | **已实测通过** |
| L3 | 平台 Agent 端到端出决策 | **不具备**，缺本地推理服务 |

---

## 5 没测的部分（要说清楚，别当成已测）

**整个容器栈从零冷启动（12 容器 + ES + Postgres）在断网条件下的表现——没测。**
原因很实际：当前演示栈正在跑（15 个容器，含 3000 / 5013 / 18000 三个端口），
把它拆掉重起到隔离网络里，成功与否都会让演示中断，风险不值得。

要补这一步，成本最低的做法是**换一台机器**（或换一套端口与 project name），按 `OFFLINE.md` 跑：

```bash
cd dist-offline/nexent-offline
./load-images.sh                     # 14 个 tar 全部 load
docker network create --internal nexent-offline
./deploy.sh                          # 注意：需把 compose 的网络改成 nexent-offline
curl -s localhost:5013/nb/v1/health  # 期望 {"status":"healthy",...}
curl -s localhost:3000/zh -o /dev/null -w '%{http_code}\n'   # 期望 200
```

期望结果：健康检查 200、前端 200；**LLM 相关请求会失败**，这是第 4 节的必然结果，不是部署错误。

---

## 6 要补什么才能说"端到端离线"

一条：**把模型指向本地推理服务**。

- Nexent 的模型工厂支持 `OpenAI-API-Compatible`，加一条模型记录指向本地 `/v1` 即可，不用改代码；
- 本地服务选 vLLM 或 Ollama 都行，需要**再打一个镜像进离线包**（当前 14 个镜像里没有）；
- 1.5B 级模型不足以稳定产出结构化决策（已有 QLoRA 结论），
  所以离线演示的正确姿势是：**用本地小模型跑通"能对话、能调工具"**，
  结论由确定性引擎给（`event_deduce` 已实测离线可跑），不要指望小模型自己算对。

**做到这一步，"离线部署"才从 L2 升到 L3。** 工作量：打 1 个镜像 + 加 1 条模型记录 + 重跑一次 §5 的清单。

---

## 7 一句话口径（说明用）

> "断网我们测过：15 个工具、六步推演、资源分配、检索全部离线可跑，输出与联网逐字一致。
> 平台层的对话编排要调大模型，现在是 4 个公网端点，断网后对话开得了但没有模型可用——
> 这一条我们写在离线说明里，接本地推理服务就能补齐，不需要改代码。"

不要说"我们完全离线可运行"——会被要求当场断网验收，而 L3 现在不成立。
