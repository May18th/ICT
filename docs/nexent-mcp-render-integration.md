# 渲染能力接入 Nexent：操作手册

> 目标不是"能连上"，而是**平台 Agent 推演完能自己拿到一段可播放的成片，并且平台拿到的就是那份结论对应的那一版**。
> 验收口径：同一次调用链里 `snapshot_id` 可核对、成片 sha256 可核对、重跑幂等。

---

## 一、这条链现在长什么样

```
Nexent Agent
   │  scene_render(snapshot_id | event_id+policy)
   ▼
RenderJob  ──► artifacts/visual/jobs/<snapshot_id>.json     （落盘即返回，delivery=pending）
   │
   │  decision_poc.render_worker  （引擎侧执行器，P11 新增）
   ▼
clips/<snapshot_id>.mp4  +  acks/<snapshot_id>.json         （delivery=acked，带 sha256）
   │
   │  scene_status(snapshot_id)                              （平台侧复查，只读）
   ▼
平台拿到 artifact.path / sha256 / fetch_url
```

改动前的空洞：`visualize.py` 明确写"不宣称渲染完成"，但**没有任何一方会写回 ack**，
所以 `delivery` 永远是 `pending`，平台调用后拿不到任何可看的东西。`render_worker` 补的就是这一环。

---

## 二、工具清单（14 个 / 4 分组）

| 分组 | 工具 | 说明 |
|---|---|---|
| decision ×9 | semantic_query … allocation_plan | 推演主链路，**看不见任何渲染工具** |
| presentation ×3 | `scene_render` | 起渲染作业 |
| | `scene_status` | 查交付状态与产物（P11 新增，只读） |
| | `scene_compare` | 两份快照差异 |
| bootstrap ×1 | `asset_lookup` | 核对 Blender 资产就绪 |
| collaboration ×1 | `collab_run` | 多智能体协同 |

分组的意义：智能体在推演循环里看不见渲染工具，就不会把推理预算花在"要不要换个镜头"上。

---

## 三、接入步骤

### 1. 生成并校验工具清单

清单是**生成产物**，不是手抄的：

```bash
PYTHONPATH=src uv run python -m decision_poc.mcp_server --export-tools samples/nexent/tools.json
# 已写出 samples\nexent\tools.json（14 个工具 / 4 个分组）

uv run pytest tests/test_nexent_tools.py -q      # 锁：清单与代码同源、必填项、禁用的渲染控制参数
```

`tests/test_nexent_tools.py` 会拦住三件事：清单漂移、工具没绑定到任何 Skill、schema 里冒出
`camera/lens/material/position/...` 这类低层参数。

### 2. 起 MCP over SSE 服务

Nexent 的「MCP 服务接入」只认 HTTP/SSE：

```bash
PYTHONPATH=src uv run python -m decision_poc.mcp_sse --host 127.0.0.1 --port 8000 \
  --visual-root artifacts/visual
# 端点：http://127.0.0.1:8000/sse
# 只暴露渲染相关：--groups presentation
```

Docker 里的 Nexent 访问宿主机用 `host.docker.internal:8000`。

### 3. 平台侧注册与装配

沿用仓库既有链路（`deploy/nexent/`）：

```bash
# 注册 MCP 服务 + 同步工具 + 建 Agent + 发布
python deploy/nexent/setup_nexent.py
# 需要把「技能」绑定到已发布 Agent 时
python deploy/nexent/bind_skills.py
```

装配完在平台界面确认：技能仓库里能看到 `scene_status`，Agent 的工具列表里有它。

### 4. 出片（引擎侧执行器）

两种模式，按现场情况选：

```bash
# A. asset：复用已渲染成片，秒级。比赛现场用这个。
PYTHONPATH=src uv run python -m decision_poc.render_worker --latest --mode asset --json

# B. blender：真起 Blender 渲新的（分钟~小时）。需要三个环境变量：
#    DECISION_POC_BLENDER_EXE / DECISION_POC_BLENDER_SCENE / DECISION_POC_BLENDER_SCRIPT
PYTHONPATH=src uv run python -m decision_poc.render_worker --latest --mode blender --force
```

`asset` 模式按 **event_id** 查预渲染表（不是按 scenario——一段片子拍的是某个具体工单的现场，
同 scenario 下不同工单的画面并不通用）。要换片子，改 `render_worker.PRERENDERED_EVENTS`，
或设 `DECISION_POC_RENDER_ASSETS=<dir>` 后放 `<event_id>.mp4`。

### 5. 验证

```bash
# 幂等：同快照再跑一次必须是 reused，不能重渲
PYTHONPATH=src uv run python -m decision_poc.render_worker --latest --mode asset --json

# 产物对账
python - <<'PY'
import hashlib, json, pathlib
ack = json.loads(next(pathlib.Path("artifacts/visual/acks").glob("*.json")).read_text(encoding="utf-8"))
clip = pathlib.Path("artifacts/visual") / ack["artifact"]["path"]
print(ack["artifact"]["sha256"] == hashlib.sha256(clip.read_bytes()).hexdigest())
PY
```

平台侧对照：调 `scene_status`，`delivery` 应为 `acked`，`artifact.sha256` 与上面一致。

---

## 四、失败模式（都设计成"不谎报"）

| 情况 | 表现 | 处理 |
|---|---|---|
| 引擎没跑 / 没出片 | `delivery=pending`，`next_action` 给出下一句该敲的命令 | 不是错误，决策照出 |
| 快照不存在 | `NOT_FOUND` | 先 `scene_render` |
| 没有对应工单的预渲染资产 | `render_worker` 退出码 2，明确说是哪个 event_id 缺 | 换 `--mode blender` 或补资产 |
| Blender 环境变量缺 | 退出码 2，逐条列出缺哪个变量 | **不静默降级成假渲染** |
| 渲染中途失败 | 不写 ack，`delivery` 保持 pending，stderr 带最后 600 字符 | 修完重跑，`--force` 可强制重来 |

---

## 五、证据清单

| 项 | 状态 | 位置 |
|---|---|---|
| 工具清单与代码同源 | ✅ 自动化测试锁死 | `samples/nexent/tools.json` + `tests/test_nexent_tools.py` |
| 渲染执行器端到端 | ✅ 7 条测试 | `tests/test_p10_render_worker.py` |
| 真机调用链输出（pending→acked→reused） | ✅ 已固化 | `docs/competition/evidence/index.html` 第 02 节 |
| 成片与 sha256 | ✅ | `docs/competition/evidence/VD02-B-incident-P9.mp4` |
| 平台界面截图（技能仓库 / Agent 工具列表） | ⬜ 待补 | 需平台实例运行 |
| 平台侧真机回归（Agent 自主调 `scene_status`） | ⬜ 待补 | 需平台实例运行 |

后两项**无法离线伪造**，必须在 Nexent 实例起来后跑一遍并截图。没跑之前不要在材料里写"已验证"。

---

## 六、回退

- 去掉渲染能力：`--groups decision,collaboration`，presentation 组不暴露，工具面上不存在渲染。
- 撤掉 `scene_status`：从 `TOOL_GROUPS["presentation"]` 删名 → 重导 `tools.json` → 删 `skills.json` 里对应条目 → 跑测试。
- `render_worker` 是独立模块，不被决策主链路 import，删掉不影响推演与评测。
