# QLoRA 扩容实验报告（2026-11-08）

## 实验设计

唯一变量是**训练数据量**：`scripts/gen_qlora_cases.py` 用 `decide()` 在 v3 参数下确定性生成
120 条（100 train + 20 holdout），标签生成前先通过「复现冻结 12 例 12/12」闸门。

四组评测，两组对照关系：

| 组 | adapter | 训练数据 | 步数 | 评测集 |
| --- | --- | --- | --- | --- |
| base | 无 | — | — | 两套都跑 |
| 24 步 | `train_adapter` | 8 例冻结 train | 24 | 冻结 4 例 / 扩容 20 例 |
| A | `gen_adapter` | 100 例生成 train | 300 | 扩容 20 例 / 冻结 4 例 |
| B | `gen_adapter_ct` | 100 例生成 train（chat 模板拼接） | 300 | 冻结 4 例 |

- A 与 24 步在**同一套扩容 holdout** 上评测 → 干净的「只改数据量」对照。
- A 与 24 步在**同一套冻结 holdout** 上评测 → 检验增益是否迁移到原始口径。
- B 只在冻结集上跑，用于验证「训练/推理格式不一致」是否是主因。

训练口径：batch=1、截断 512、梯度检查点、lr=2e-4、r=8/alpha=16、AdamW。
loss：A 0.4956→0.0647→0.0570（峰值显存 2374.9MB）；B 0.5322→0.0599→0.0536（2434.3MB）。

## 结果一：扩容 holdout（20 例，同一评测集）

| 指标 | base | 24 步（8 例） | A（100 例） |
| --- | --- | --- | --- |
| json_valid | 0% | 60% | **100%** |
| allocation_by_district | 0% | 0% | 35% |
| transfers | 0% | 10% | **60%** |
| priority_order | 0% | 0% | **85%** |
| critical_unmet | 0% | 5% | 35% |

数据量从 8 → 100，`transfers` 10%→60%、`priority_order` 0%→85%、`json_valid` 60%→100%。
**在训练分布内，扩容确实解决了 schema 对齐问题。**

## 结果二：冻结 holdout（4 例，原始口径）

| 指标 | base | 24 步 | A（100 例） | B（chat 模板） |
| --- | --- | --- | --- | --- |
| json_valid | 0% | 75% | 100% | 100% |
| allocation_by_district | 0% | 0% | 25% | 25% |
| transfers | 0% | 25% | **0%** | **0%** |
| priority_order | 0% | 0% | 0% | 0% |
| critical_unmet | 0% | 0% | 0% | 50%※ |

※ B 的 50% 含一例误中：H3 期望 `{}`，模型输出空字典 `{}` 而命中，实际只对了 H4 一例。

**增益没有迁移。** A 的 `transfers` 从 25% 掉到 0%，`priority_order` 仍是 0%。
「瓶颈是数据规模」这个假设**未被证实**——被证实的是「模型能拟合生成分布」。

## 失败模式（原始输出）

A 在冻结集上四条全部只吐一个字段就闭合：

    H1 → {"allocation_by_district": {"A-03": 3, "B-01": 2}}
    H2 → {"allocation_by_district": {"A-03": 2, "B-01": 4}}
    H3 → {"allocation_result": {"A-03": 2, "B-01": 2}}      ← 键名漂移
    H4 → {"allocation_by_district": {"A-03": 3, "B-01": 3}}

H1 的 `{A-03:3, B-01:2}` 恰好等于「各片区只用本区库存」的 v1 口径
（A-03 需求 4 库存 3、B-01 需求 2 库存 4）——即模型**根本没学会跨区调拨分支**，
退化回了默认策略。

B 补上 chat 模板后字段变多、输出变长，但键名系统性写成 `order` 而非 `priority_order`：

    H1 → {..."order": ["A-03", "B-01"], "transfers": []}     ← 该调拨却给空数组

调拨规则本身仍未学到。**格式对齐不是主因。**

## 归因

生成器的 `title` / `basis` 是**统一模板**（`生成用例 001：…` / `信号：…`），
而冻结集是人手写的措辞（`实况资源紧张：…` / `Ground Truth 跳 5：…`）。
模型学到的是「看到『生成用例』这种措辞就吐四个键」的表面关联，换措辞即崩。
这是我在这份生成器里引入的设计缺陷，不是数据量不够。

次要因素：`qlora_train.py` 的训练文本是 user/assistant 直接换行拼接，标签不 mask，
与 `eval_qlora.py` 推理侧的 `apply_chat_template` 本就不一致（B 变体已验证非主因，但仍是缺陷）。

## 结论

1. 扩容能教会 schema **在训练分布内**对齐（json_valid 100%、transfers 60%、priority 85%）。
2. 扩容**不能**让规则迁移到人手写的措辞分布；跨区调拨规则始终没学会。
3. 因此当前瓶颈不是数据量，是**生成分布与目标分布的表层差异**（措辞模板单一）。
4. 本地 1.5B 仍不承担生产推理，原口径不变（DeepSeek 带工具主链路）。

## 下一步建议（二选一）

- **A：提高生成分布的措辞多样性**——title/basis 按 6~8 种句式随机化，
  并把冻结 8 例 train 混入训练集（holdout 不动），再测冻结 4 例。
- **B：换任务形式**——不再让模型算数，改为让模型只输出「结构化中间表示」
  （谁关键、谁让出、让多少），把算术交给代码。这与生产路径的选择更一致。

## 复现命令

```bash
python scripts/gen_qlora_cases.py
python scripts/build_qlora_dataset.py --source dist/qlora/allocation_cases_gen.json --out dist/qlora/gen
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv-qlora/Scripts/python.exe scripts/qlora_train.py \
    --dataset dist/qlora/gen/train.jsonl --out dist/qlora/gen_adapter --epochs 3
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv-qlora/Scripts/python.exe scripts/eval_qlora.py \
    --cases dist/qlora/allocation_cases_gen.json --adapter dist/qlora/gen_adapter --out-stem eval_genA
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv-qlora/Scripts/python.exe scripts/eval_qlora.py \
    --adapter dist/qlora/gen_adapter --out-stem eval_genA_frozen
```

注：本机代理会拦 HF 请求（`httpx2.ProxyError: 502`），必须带 `HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1`。
模型缓存在 `~/.cache/huggingface/hub/models--Qwen--Qwen2.5-1.5B-Instruct`。

---

# 追加实验：IR 方案（拆出算术，模型只做离散判定）

## 设计

`scripts/qlora_ir.py` 把一次决策拆成两段：

    模型：signals ──> IR {critical_districts, priority_order, transfers[{from,to}]}
    代码：IR + signals ──> 完整分配（台数、迟延、缺口）

IR **不含任何数字**，台数由 `apply_ir()` 按 decide() 同款算术补齐。
充分性闸门：从 decide() 反推 IR 再回放，冻结 12 例逐字段一致（**12/12 通过**），
证明 IR 无损，模型再准也能还原出正确分配。

## 三组配置（均评测冻结 holdout 4 例）

| 配置 | 训练数据 | 轮次 | ir_valid | critical | priority | transfer方向 | allocation |
| --- | --- | --- | --- | --- | --- | --- | --- |
| C1 | 生成 100 | 3 | **0%** | 25% | 0% | 25% | 25% |
| C2 | 生成 100 + 冻结 8 train | 3 | 100% | 75% | 50% | 50% | 50% |
| C3 | 生成 100 + 冻结 8 train | 8 | 100% | 75% | **75%** | 50% | 50% |

## 关键修正：50% 是虚的

`transfer_direction` 的 50% 全部来自**本来就不需要调拨**的 H3/H4（输空数组即命中）。
按是否真的需要调拨拆开看：

| 案例 | 是否需调拨 | C2 | C3 |
| --- | --- | --- | --- |
| H1 | 需要（B-01→A-03） | ❌ 输出 `[]` | ❌ 输出 `[]` |
| H2 | 需要（A-03→B-01） | ❌ 输出 `[]` | ❌ 输出 `[]` |
| H3 | 不需要 | ✅ | ✅ |
| H4 | 不需要 | ✅ | ✅ |

**需要调拨的 0/2。**

## 结论

1. IR 拆解确实起效：`ir_valid` 从 0% 到 100%，`priority_order` 从 0% 到 75%，
   关键性判定到 75%。这些是 300 步全 JSON 方案（transfer 0%、priority 0%）做不到的。
2. **训练集必须含目标分布措辞**：C1 只用生成集时，模型在冻结措辞上直接改说中文散文，
   `ir_valid` 归零。并入冻结 8 例 train 后立刻稳定——这是比「数据量」更强的变量。
3. **调拨判定始终学不会**。五组配置（24 步 / 300 步 A / 300 步 B chat 模板 / IR C2 / IR C3 864 步）
   没有一次在需要调拨的用例上输出过非空 `transfers`。模型稳定退化到「不调拨」这个保守分支。
4. 加轮次只改善优先序（50%→75%），不改变调拨判定；C3 在 H2 上甚至把关键片区判反了（说 A-03，实为 B-01），
   说明它仍在套「A-03 优先」的模板。

## 判断

模型能学会**格式、关键性、优先序**，学不会**「该调拨」这个联合判定**。
五组独立配置一致失败，可以停止在这条路上继续调数据了。
生产推理仍由 DeepSeek 带工具主链路承担；本地 1.5B 的定位保持为管线验证与对照方法。

## 复现命令（IR）

```bash
python scripts/qlora_ir.py                      # IR 充分性闸门，应输出 12/12
python scripts/build_qlora_ir_dataset.py --out dist/qlora/ir_gen
python scripts/build_qlora_ir_dataset.py --frozen-train --out dist/qlora/ir_mix
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv-qlora/Scripts/python.exe scripts/qlora_train.py \
    --dataset dist/qlora/ir_mix/train.jsonl --out dist/qlora/ir_adapter_mix --epochs 8
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 .venv-qlora/Scripts/python.exe scripts/eval_qlora_ir.py \
    --adapter dist/qlora/ir_adapter_mix --out-stem ir_C3_frozen
```

环境补充：`.venv-qlora` 原先没有 pydantic（IR 模块要导入 decision_poc），
已用 `uv pip install --python .venv-qlora/Scripts/python.exe pydantic` 补上。

