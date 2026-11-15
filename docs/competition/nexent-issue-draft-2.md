# Nexent 平台 issue 草稿 #2：code 评估器沙箱约束未文档化

> 待提交。与已提交的 #4307（技能-工具一致性 lint）不同，这条针对**评估器（evaluator）沙箱**。
> 提交账号 `May18th`，仓库 `ModelEngine-Group/nexent`。

---

## 一、可直接贴的版本（约 300 词）

**Title**

`[Improvement] Document and surface code-evaluator sandbox constraints (no import, no dunder, module-level names unreachable)`

**Body**

### Summary

The `code` evaluator type is a powerful way to write deterministic scoring, but its sandbox has three non-obvious
constraints that are only discoverable by trial-and-error at authoring time:

1. `import` statements are rejected (`__import__ not found`) — only `json` and a builtin whitelist are injected as globals.
2. Any dunder attribute access (e.g. `type(exc).__name__`) is rejected by the AST scan (`dunder attribute access`).
3. Module-level names (constants, helper functions) defined at the top of the evaluator body are **unreachable** from
   `evaluate()` at runtime — they live in the exec `locals`, while `evaluate()` resolves globals against the injected
   `{"__builtins__": ..., "json": ...}` dict, so it raises `NameError: name 'X' is not defined` even though the name
   exists in the same file.

All three fail with terse, generic messages (`__import__ not found`, `Code rejected — forbidden operations detected`,
`Code execution failed during validation`) that do not tell the author *how to write valid code*.

### Environment

Nexent `v2.7.0`, docker compose. Evaluator created via `POST /api/evaluators` with `evaluator_type=code`.

### Expected

Either the authoring UI / API response documents the sandbox contract, or validation errors point to the specific rule
violated and the supported workaround (define everything inside `evaluate()`, use the injected `json` global, avoid
dunder attributes).

### Actual

Authors discover the three rules one at a time through rejection messages, none of which mentions the actual contract.
We iterated three times (`__name__` removal → `import json` removal → moving constants into the function body) before
the evaluator validated.

### Cause

`backend/services/agent_evaluation_service.py`:
- `validate_code_evaluator()` runs `exec(compile(code, ...), {"__builtins__": ALLOWED_BUILTINS, "json": json}, local_vars)`,
  so `import` fails (no `__import__` in `ALLOWED_BUILTINS`) and top-level assignments land in `local_vars`, not the
  function's globals.
- `_scan_evaluator_introspection()` rejects dunder attribute/name access as a sandbox-escape guard.

These are correct *security* decisions; the gap is purely in discoverability.

### Proposal

Minimal, additive:
1. Document the three rules in the evaluator authoring UI and/or the `POST /api/evaluators` error path (a `hint` field
   or a link to docs).
2. Make the three error messages actionable: e.g. `import` → "only the `json` module and a builtin whitelist are
   available; define helpers inside `evaluate()`"; `NameError` during validation → "module-level names are not visible
   to `evaluate()` in the sandbox; nest them inside the function".

No behaviour change to the sandbox itself.

### Impact

A team using `code` evaluators for deterministic regression scoring (our use case: replicating a weighted
allocation-deviation formula) loses time to three avoidable rejection rounds. The workaround (nest everything inside
`evaluate()`) is simple but not discoverable from the current messages.

---

## 二、我们这边的口径（不进 issue，答辩用）

- 这条和 #4307 一样，是「平台缺一层可发现性/文档」，不是「平台有 bug」。措辞用 improvement，不指责。
- 价值点：我们**真的在平台上写了一个 code 评估器**（`evaluator_id=16`），并踩出了这三个沙箱约束，
  所以这条 feedback 是"真实使用 + 真实踩坑"的产物，不是凭空提建议。
- 三条坑里，第 3 条（模块级变量不可达）是最有价值的一条——它违反 Python 作者直觉（同文件里定义的名字
  应该能访问），是最容易被误判成"平台坏了"的点。
- 答辩一句话：**「我们在平台上写了确定性评估器跑进化对比，顺手把评估器沙箱的三个未文档化约束提回给了平台。」**

## 三、提交前要确认

- [ ] 是否有 maintainer 在 issue 模板里要求 repro 步骤更完整（补充一个最小可复现的 code 片段）
- [ ] 三个 error message 的原文是否准确（从本次实测日志里摘，不要凭记忆写）
