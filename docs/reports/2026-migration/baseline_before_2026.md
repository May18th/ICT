# 2026 迁移 · 迁移前基线（2026-11-03）

> 用途：Task #2 第二步（资产 17→18 + 冻结数字同步）的对照基线。**只记录，不改任何数字。**

## 当前口径
| 项 | 值 |
|---|---|
| 对外数字自检 | **106/106 项一致**，期望指纹 `db4af83c9449a498` |
| 测试 | `743 passed` |
| 期望文件 | `samples/eval/evidence_expectations.json` |
| urban 资产数（manifest） | 17 |
| 共享规则数 | 38（其中真实来源 6） |

## P8 资产口径（迁移后按实测同步）
- `assets_total` / `assets_parsed` 的期望值位于 `samples/eval/evidence_expectations.json`，迁移前实测值由
  `scripts/run_evidence_check.py`（或等价入口）输出的 `p8_eval` 段给出：当前自检 **106/106 全部一致**，
  说明期望 == 实测；迁移后**先跑 P8 取实测**，再改期望，不允许反向迁就实现。

## 迁移后需同步的位置（清单，本文件不修改）
1. `samples/eval/evidence_expectations.json` → `p8_eval.assets_total/assets_parsed`（按 P8 实测）；
2. `README.md:123/377/388`（资产口径；**不要动 89/94 的 17/17 步骤数**）；
3. `docs/competition-gap-audit.md:32`；
4. `samples/assets/urban/README.md` 资产表；
5. `tests/test_urban_dataset.py:24`、`tests/test_urban_eval.py:74`（**不要动 :120 的 17/22 用例数**）；
6. 若加“observed 定级白名单”守护测试 → 743→744，须同一次提交同步；
7. 重生成 `evidence-check-report.md` 与新指纹。

## 迁移前检查
- [x] 工作区 clean、HEAD 与 origin/main 一致
- [x] 规则草案（39 条口径）与候选资产已归档 `docs/reports/2026-migration/`
- [x] 2026/2020 差异表核对（区县 8、预警上限三级、一级无持续句、四级 6h、二级 8 区县 24h150）
- [ ] 作品提交完成（迁移触发条件）
