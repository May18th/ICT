# 暂缓纳入主干的测试

这里存放**代码尚未纳入主干**的测试。默认不被 pytest 收集（见同目录 `conftest.py` 的
`collect_ignore_glob`），因此不影响 `test_baseline.collected` 这个对外数字。

## 当前内容

| 文件 | 对应模块 | 暂缓原因 |
| --- | --- | --- |
| `test_rag.py` | `src/decision_poc/rag/`（未跟踪） | RAG 检索层未接入主链路 |

## RAG 模块为什么暂不纳入主干

1. **未接入主链路**——考点③要的是「检索-推理双驱动」，但六跳里的 `evidence_locate`
   目前走的是资产台账定位符，不是语义检索，检索这半边还是空的；
2. **依赖口径未定**——`rag/` 需要 `numpy`，而主干 `pyproject.toml` 没有这个依赖。
   补进去等于为一个尚未被调用的模块增加一个顶层依赖，会让"主干装什么"这件事失真；
3. **对外数字必须可复现**——`test_baseline.collected` 是评委 clone 仓库后能当场跑出来的数。
   把未纳入模块的测试计进去，数字就是虚高的。

## 恢复条件

满足以下两条后，把 `test_rag.py` 移回 `tests/` 并删除本目录的 `conftest.py`：

1. `rag/` 接入 `evidence_locate`，六跳的检索环节换成语义检索且评测口径固定；
2. `numpy` 作为正式依赖写进 `pyproject.toml`（连同 `uv.lock` 一起 `uv lock`）。

移回后必须**一次改全**对外数字：`samples/eval/evidence_expectations.json` 的
`test_baseline.collected` 与 `checks.test_suite.docs_declared`、README、`competition-gap-audit.md`、
`preliminary-submission-draft.md`，再重跑 DOCX/PPTX 生成器，最后 `evidence_check` 取新指纹并同步。

## 这 6 个测试锁的是什么

`test_rag.py` 的 6 条用例不是覆盖率填充，每条对应一个「跑起来不报错、但数字是错的」缺陷：

1. 副本目录（`.worktrees`/`.superpowers`/`artifacts`/`deploy-webdemo`）不得进入语料；
2. `recall@10` 必须落在 `[0,1]`（曾算出 1.8）；
3. 检索结果里同一个 `chunk_id` 只占一个结果位；
4. `compare()` 的「不精排」行必须拿到 `IdentityReranker` 实例而非 `None`（机制断言）。

接入时建议连同这份清单一起看——它们是那次修复的验收依据。
