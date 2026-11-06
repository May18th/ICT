# 真实用户评分采集（第二反馈信号的真实版）

## 0 一句话

P9.1 的人工评审是**角色扮演**（`samples/eval/decision_reviews.json` 里固定标注"非真实部门批复"），
本模块把"真实用户评分"做成**可采集、可汇总、可导出**的通道：3–5 名真实用户（评委/同学/领域从业者）
在进化前后各评一次，评分进汇总，带修正建议的评分转成评审用例进 P9 门禁。

## 1 两条通道，口径分开

| 通道 | 输入 | 用途 | 是否参与门禁 |
|---|---|---|---|
| 评分通道 | 四维 1–5 分（准确性/时效性/安全性/可解释性）+ 意见 | 样本数、均分、进化前后差值、分布 | **否**。分数是主观信号，不能自动复算成"候选策略对不对" |
| 修正通道 | 评分 + 可选"建议分配"（如 `A-03=5,B-01=1`） | 转成 `ReviewRecord(MODIFY)` → 评审用例 → 门禁硬约束 | **是**。候选不满足该分配即被拒，与 P9.1 同一条路 |

给修正建议时，服务器会把**当时的决策输入快照**（每个片区的深度/是否封路/是否有危险源/需求/本区库存）
一并写进提交记录，保证评审用例**自包含**——下一轮评测不必回跑推演，别人拿到记录就能复算。

## 2 怎么用

### 2.1 浏览器（推荐，答辩现场可让评委当场打分）

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -m decision_poc.web_demo --port 8787
# 打开 http://127.0.0.1:8787/evolution → 页面中"真实用户评分"卡片
```

表单字段：姓名/编号（必填）、身份、进化前/进化后、四维评分（如 `4,3,5,4`）、
可选建议分配（如 `A-03=5,B-01=1`）、意见。提交后页面立即回显样本数/均分/前后差值/可进门禁条数。

**提交「进化后」评分时会顺手跑一次进化门禁**：后端用当前评分（含带修正建议的评审用例）重跑一次 `policy_evolution.evolve`（不写盘、不碰记忆），页面直接回显最终策略标签、轮次与采纳/拒绝数、训练/留出偏差前后值、生效评审 ID——评委当场打分就能看到「评分 → 门禁 → 候选采纳/拒绝」的闭环。

门禁结果会**落盘并保留历史**：`artifacts/evolution/latest.json`（最近一次）+ `history.jsonl`（追加式），页面通过 `GET /api/evolution/gate?limit=3` 读回，显示最终标签、采纳/拒绝数、训练与留出偏差、累计条数；可用环境变量 `URBAN_TWIN_EVOLUTION_DIR` 改目录（测试用临时目录，不污染仓库）。

另外，`policy_evolution --allow-combinations` 会在选优时并列评估**组合候选**（同时改动 ≥2 个参数，如「封路优先 + 跨区成本」），用于验证门禁对「多点同时改动」的拦截；该开关默认关闭，已冻结的 P9 数字（v1.2 / 3 轮 / 0.2264→0）不受影响。

### 2.2 命令行

```powershell
$env:PYTHONPATH = "src"

# 只评分（不进门禁）
uv run --locked python -m decision_poc.review_collect add --reviewer u1 --role "环境专业教师" `
    --phase before --scores 4,3,5,4 --comment "结论清楚，但道路封闭依据可以再点到条款"

# 评分 + 修正建议（进门禁）
uv run --locked python -m decision_poc.review_collect add --reviewer u2 --role "市政从业者" `
    --phase after --scores 5,4,5,5 --suggested "A-03=5,B-01=1" --comment "B-01 至少留 1 台兜底"

uv run --locked python -m decision_poc.review_collect summary --markdown   # 汇总
uv run --locked python -m decision_poc.review_collect to-reviews           # 导出评审用例
```

### 2.3 接入进化门禁

`to-reviews` 导出的文件与 `samples/eval/decision_reviews.json` **同构**（`schema_version=decision-reviews-1`），
可直接交给 P9：

```python
from decision_poc.experience import load_reviews
from decision_poc.policy_evolution import evolve

reviews = load_reviews("samples/eval/review_gate_reviews.json")
report = evolve(..., reviews=reviews)   # 评审用例进入门禁：候选违反即被拒
```

## 3 落盘与隐私

- 默认路径 `samples/eval/review_submissions.jsonl`（**追加式**，不覆盖历史；同内容重复提交幂等）；
- 可用环境变量 `URBAN_TWIN_REVIEWS` 指到别处（例如本机不提交的真实数据目录）；
- 该文件默认**不进 git**（真实用户姓名/意见属于个人信息）；
- 记录里 `note` 固定写「真实用户评分（个人意见，非部门批复）」，报告与答辩口径必须一致。

### 记录结构（一行一条）

```json
{
  "schema_version": "decision-review-submissions-1",
  "submission_id": "RV-<内容哈希12位>",
  "created_at": "2026-10-31T10:00:00+00:00",
  "reviewer_id": "u2",
  "reviewer_role": "市政从业者",
  "phase": "after",
  "event_id": "WO-20261029-007",
  "as_of": "2026-10-29T09:40:00+08:00",
  "policy_version": "v1",
  "scores": {"accuracy": 5, "timeliness": 4, "safety": 5, "explainability": 5},
  "comment": "B-01 至少留 1 台兜底",
  "suggested_allocation": {"A-03": 5, "B-01": 1},
  "signals": [{"district": "A-03", "depth_cm": 30.0, "closure_required": true, ...}],
  "current_allocation": {"A-03": 4, "B-01": 2},
  "source": "web",
  "note": "真实用户评分（个人意见，非部门批复）"
}
```

## 4 汇总指标

`summary`（JSON）与 `summary --markdown` 输出：

- 样本数、评审人数、是否满足最低样本（≥3 位且进化前后各有样本）；
- 进化前/后 综合均分与四维均分、前后差值；
- 评分分布、有文字意见条数；
- 「可进门禁」条数（= 带修正建议的提交数）。

## 5 诚实边界（答辩必须说清楚）

1. 评分是**个人意见**，不是部门批复；不能表述为"真实部门反馈"或"真实市政数据回流"；
2. 评分只做**汇总**，不作为门禁硬约束——分数无法自动复算成"候选对不对"；
3. 只有"带修正建议"的提交转成评审用例进门禁，且只评"谁拿几台"（transfer/order 维度不评，见 P9 设计）；
4. 现有仓库里没有真实评分样本；空文件时 `summary.total = 0`，`meets_min_reviewers = false`，
   不得把空样本说成"已有真实反馈"。

## 6 与既有模块的关系

- `decision_poc.experience`：评审记录/评审用例的模型（本模块复用 `ReviewRecord`）；
- `decision_poc.policy_evolution`：门禁与进化内核（`review_cases_for` 把评审用例并入评测）；
- `decision_poc.web_demo`：`/api/reviews` 的 GET/POST 端点与 `/evolution` 页面表单；
- `decision_poc.review_collect`：采集、汇总、导出与 CLI。

## 7 验收

- `uv run --locked pytest -q`：835 项通过（含 17 条本通道用例：模型校验、幂等、汇总、转评审用例、导出、CLI、Web 路由）；
- `python -m decision_poc.evidence_check`：108/108 项一致（测试数已同步）。
