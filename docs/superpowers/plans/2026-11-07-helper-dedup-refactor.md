# Helper Deduplication Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove confirmed duplicate helper implementations while preserving all existing behavior and public import paths.

**Architecture:** Add a small internal utility module for canonical JSON, SHA-256 digests, datetime/number coercion, and dataset-root/CSV loading. Keep domain-specific wrappers only where their input or output contract differs. Keep service-local logging and encoding helpers unchanged.

**Tech Stack:** Python 3.12, pytest, pathlib, csv, hashlib, datetime.

**Spec:** `docs/hydro-integration-refactor-candidates.md` and the user-approved D1–D6 candidate list.

## Global Constraints

- No new product behavior or dependency changes.
- Preserve current public imports and serialized outputs.
- Do not touch the pre-existing `src/decision_poc/web_demo.py` worktree change.
- Keep D7/D8 deferred because their service/data boundaries differ.

## Review Focus

- Canonical JSON must keep `ensure_ascii=False`, sorted keys, and compact separators.
- Datetime and number coercion must preserve current `None` behavior on invalid input.
- CSV readers must keep UTF-8 BOM handling and missing-file behavior.
- `ErrorCode` fallback must remain `INTERNAL_ERROR`.
- Existing module-local helper imports must continue to work where tests or callers use them.

### Task 1: Add shared pure helpers and regression tests

**Files:**
- Create: `src/decision_poc/_shared.py`
- Test: `tests/test_shared_helpers.py`

- [ ] Write tests for canonical JSON/digest, datetime/number coercion, dataset root, and CSV reading.
- [ ] Run the focused tests and verify they fail because the module is absent.
- [ ] Implement the minimal helpers.
- [ ] Run the focused tests and verify they pass.

### Task 2: Replace exact duplicate helpers

**Files:**
- Modify: `src/decision_poc/experience.py`
- Modify: `src/decision_poc/review_collect.py`
- Modify: `src/decision_poc/counterfactual.py`
- Modify: `src/decision_poc/deduction.py`
- Modify: `src/decision_poc/skills.py`
- Modify: `src/decision_poc/contracts.py`
- Test: existing experience, review, counterfactual, deduction, and skills tests

- [ ] Replace D2–D5 implementations with shared helpers while retaining local aliases only if existing imports require them.
- [ ] Add the shared `ErrorCode` coercion helper without changing enum fallback behavior.
- [ ] Run focused tests.

### Task 3: Replace dataset-root and CSV-reader duplicates

**Files:**
- Modify: `src/decision_poc/baseline_compare.py`
- Modify: `src/decision_poc/chain.py`
- Test: existing baseline/chain tests

- [ ] Replace D1 and D6 with shared helpers.
- [ ] Preserve `utf-8-sig`, missing-file empty-list behavior, and row types.
- [ ] Run focused tests.

### Task 4: Full verification and review

- [ ] Run `uv run --locked pytest -q`.
- [ ] Run `uv lock --check`.
- [ ] Run `uv run --locked python -m compileall -q src tests`.
- [ ] Run `uv pip check --python .venv\Scripts\python.exe`.
- [ ] Run `git diff --check` and inspect the diff for unrelated changes.
- [ ] Keep the existing `web_demo.py` diff out of the refactor commit.
