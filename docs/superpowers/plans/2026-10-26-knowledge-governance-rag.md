# Knowledge Governance and RAG Evidence Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an offline, deterministic knowledge-governance and retrieval evidence loop to the generic decision PoC, including permission/freshness filtering, explicit conflict review, and retrieval evaluation.

**Architecture:** Add domain-neutral Pydantic contracts and an in-memory `EvidenceProvider` implementation. LangGraph remains the only orchestrator and continues to receive JSON-serializable tool envelopes; conflict and retrieval failure paths terminate safely before decision generation or simulated execution. LangChain remains unchanged at the adapter boundary.

**Tech Stack:** Python 3.12, Pydantic 2.14.1, LangGraph 1.2.17, pytest 9.1.1; no new dependencies.

**Spec:** `docs/superpowers/specs/2026-10-26-knowledge-governance-rag-design.md`

## Global Constraints

- Do not connect a real Wiki, vector database, embedding provider, model provider, Nexent service, or production tool.
- Keep LangGraph as the only workflow orchestrator; do not add another Agent Loop.
- Preserve the existing `ToolResult` envelope and JSON-serializable graph state.
- Keep domain-specific schemas, retrieval thresholds, and industry metrics out of this phase.
- Human approval is simulated input only; this phase does not implement identity, authorization, checkpoint persistence, or a UI.
- Knowledge content is never automatically edited or published by the agent.

## Review Focus

- Expired, draft, or deprecated items must not appear in results; test status and both validity-time boundaries.
- Unauthorized items must never leak through retrieval, including when they rank above an authorized match; test permission filtering before ranking/top-k.
- Explicitly conflicting retrieved items must stop before decision generation and execution; test conflict state and reviewer choices.
- Empty retrieval and malformed provider envelopes must produce structured terminal results rather than fabricated evidence or uncaught graph errors.
- Evidence citations must retain stable knowledge IDs and source/version identity through decision validation; test mismatched IDs, versions, and unknown citations.

## File Structure

- `src/decision_poc/contracts.py`: add `KnowledgeItem`, `EvidenceQuery`, and retrieval review enums/fields; extend `EvidenceItem` with optional stable knowledge reference metadata.
- `src/decision_poc/knowledge.py`: define the provider protocol and deterministic in-memory provider, including governance filtering, keyword matching, and explicit conflict grouping.
- `src/decision_poc/graph.py`: accept an optional provider and simulated conflict-review input; route retrieval failures and conflict review before decision generation.
- `src/decision_poc/evaluation.py`: add retrieval metric aggregation without changing existing technical summary semantics.
- `tests/test_knowledge.py`: cover contracts, filtering, ordering, and conflict identification.
- `tests/test_decision_flow.py`: cover graph terminal and human-review branches and evidence identity validation.
- `tests/test_evaluation.py`: cover retrieval metric aggregation and permission-safety reporting.
- `README.md`, `docs/README.md`, and `docs/technical-baseline.md`: document the offline knowledge path and its limits.

---

### Task 1: Define knowledge contracts and deterministic evidence provider

**Files:**
- Modify: `src/decision_poc/contracts.py`
- Create: `src/decision_poc/knowledge.py`
- Test: `tests/test_knowledge.py`

**Interfaces:**
- `KnowledgeItem` includes stable ID, title/content, topic path, source/version, publication status, owner, validity interval, visibility labels, and explicit `conflicts_with` IDs.
- `EvidenceQuery` includes request ID, query text, optional topic prefix, caller visibility labels, `as_of`, `top_k`, and retrieval mode `keyword`.
- `EvidenceProvider.retrieve(query: EvidenceQuery) -> ToolResult`.
- `InMemoryEvidenceProvider(items: Sequence[KnowledgeItem]).retrieve(query) -> ToolResult`.
- Retrieved `EvidenceItem` carries optional `knowledge_id` and validity metadata while remaining compatible with existing three-field construction.
- The in-memory provider filters for published, valid, and caller-visible items before deterministic keyword scoring; tie-break by stable `knowledge_id`.
- Conflict is recognized only through explicit `conflicts_with` links among eligible retrieved items. This phase does not infer semantic contradictions.

- [ ] **Step 1: Write failing tests for contract validation and provider behavior**
  - Add `test_knowledge_item_rejects_invalid_validity_interval`.
  - Add `test_provider_excludes_unpublished_expired_and_unauthorized_items`.
  - Add `test_provider_filters_permissions_before_top_k_ranking`.
  - Add `test_keyword_provider_returns_deterministic_ranked_evidence`.
  - Add `test_provider_marks_explicit_conflicts_in_result_context`.

- [ ] **Step 2: Run focused tests and confirm expected failures**

Run: `uv run --locked pytest -q tests/test_knowledge.py`

Expected: FAIL because the knowledge contracts and provider do not exist yet.

- [ ] **Step 3: Implement the contracts and deterministic provider**
  - Use timezone-aware datetimes and the interval rule `valid_from <= as_of < valid_until` when bounds are present.
  - Use token/substring keyword matching with deterministic tie-breaking; do not introduce embeddings or external services.
  - Return source, version, knowledge IDs, validity metadata, conflict IDs, and structured empty/error status in the existing `ToolResult` shape.

- [ ] **Step 4: Run focused tests**

Run: `uv run --locked pytest -q tests/test_knowledge.py`

Expected: all provider and contract tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/decision_poc/contracts.py src/decision_poc/knowledge.py tests/test_knowledge.py
git commit -m "feat: add governed in-memory evidence provider"
```

### Task 2: Route retrieval and conflict review through LangGraph

**Files:**
- Modify: `src/decision_poc/graph.py`
- Modify: `src/decision_poc/demo.py`
- Test: `tests/test_decision_flow.py`

**Interfaces:**
- `run_decision_async(..., evidence_provider: EvidenceProvider | None = None, conflict_review: ConflictReview | None = None, ...)`.
- `run_decision(..., evidence_provider: EvidenceProvider | None = None, conflict_review: ConflictReview | None = None, ...)`.
- `ConflictReview` values are `continue`, `reject`, and `supplement`; `supplement` requires an explicit list of supplemental `EvidenceItem` values.
- No provider argument preserves the existing scripted simulation behavior for backward compatibility.
- With a provider, the graph creates an `EvidenceQuery` from the request and filters decision evidence against the exact retrieved `knowledge_id`, source, version, and content tuple.

- [ ] **Step 1: Write failing tests for graph routing and review decisions**
  - Add `test_empty_knowledge_retrieval_terminates_without_generating_decision`.
  - Add `test_conflict_without_review_pauses_before_decision_and_execution`.
  - Add `test_conflict_review_rejected_terminates_without_execution`.
  - Add `test_conflict_review_continue_records_reviewer_choice_before_decision`.
  - Add `test_conflict_review_supplement_requires_and_validates_extra_evidence`.
  - Add `test_decision_citation_must_match_knowledge_id_source_version_and_content`.
  - Preserve current simulated approval requirements for actions after decision generation.

- [ ] **Step 2: Run focused tests and confirm expected failures**

Run: `uv run --locked pytest -q tests/test_decision_flow.py`

Expected: FAIL because provider injection, conflict routing, and knowledge identity validation are not implemented.

- [ ] **Step 3: Implement provider injection and safe review routing**
  - Keep graph state JSON-compatible; serialize the query, provider result, reviewer choice, and evidence only.
  - Return distinct terminal reasons for evidence retrieval failure, no eligible evidence, and unresolved/rejected conflict review.
  - Missing conflict review pauses before generating a decision; `continue` proceeds only on explicit caller input; `reject` terminates; `supplement` validates supplied evidence before proceeding.
  - Do not treat simulated review input as an authenticated identity assertion.

- [ ] **Step 4: Run focused tests**

Run: `uv run --locked pytest -q tests/test_decision_flow.py`

Expected: all existing and new decision-flow tests pass.

- [ ] **Step 5: Commit**

```powershell
git add src/decision_poc/graph.py src/decision_poc/demo.py tests/test_decision_flow.py
git commit -m "feat: add retrieval and human conflict review routing"
```

### Task 3: Add retrieval evaluation and project documentation

**Files:**
- Modify: `src/decision_poc/evaluation.py`
- Modify: `src/decision_poc/baseline.py`
- Test: `tests/test_evaluation.py`
- Test: `tests/test_baseline.py`
- Modify: `README.md`
- Modify: `docs/README.md`
- Modify: `docs/technical-baseline.md`

**Interfaces:**
- Evaluation summaries report retrieval hit, citation/source-version match, freshness, permission safety, explicit-conflict detection, and evidence sufficiency as counts/ratios over supplied cases.
- Existing summary fields and current three baseline scenarios remain available; add separate knowledge scenarios without claiming industry performance.

- [ ] **Step 1: Write failing aggregation and baseline tests**
  - Add `test_evaluation_aggregates_retrieval_quality_and_permission_safety`.
  - Add `test_knowledge_baseline_covers_hit_empty_conflict_and_expired_cases`.

- [ ] **Step 2: Run focused tests and confirm expected failures**

Run: `uv run --locked pytest -q tests/test_evaluation.py tests/test_baseline.py`

Expected: FAIL because retrieval-specific metrics and knowledge baseline cases are not present.

- [ ] **Step 3: Implement metrics, deterministic baseline cases, and documentation**
  - Keep metrics tied to explicit fixture ground truth; do not infer domain correctness from retrieval scores.
  - Document the knowledge governance/RAG relationship, provider boundary, review behavior, offline-only status, and commands to run the baseline.

- [ ] **Step 4: Run full verification**

Run:

```powershell
uv run --locked pytest -q
uv lock --check
uv run --locked python -m compileall -q src tests
uv pip check --python .venv\Scripts\python.exe
```

Expected: all tests pass; lockfile and dependencies are consistent; sources compile.

- [ ] **Step 5: Commit**

```powershell
git add src/decision_poc/evaluation.py src/decision_poc/baseline.py tests/test_evaluation.py tests/test_baseline.py README.md docs/README.md docs/technical-baseline.md
git commit -m "feat: evaluate governed knowledge retrieval"
```

## Self-Review

- Spec coverage: knowledge metadata/provider (Task 1); filtering, conflicts, graph state and safe failure (Tasks 1–2); reviewer choices and citation consistency (Task 2); evaluation and public docs (Task 3); no real integrations or production side effects (all tasks).
- Review focus has explicit tests in Task 1 and Task 2; task 3 tests aggregate retrieval outcomes.
- Interfaces are aligned: `EvidenceQuery` is produced by the graph and consumed by `EvidenceProvider`; provider output is a `ToolResult`; `EvidenceItem` carries the identity fields used by graph validation.
- Scope is intentionally limited to deterministic keyword matching and explicitly declared conflict links. Semantic contradiction detection and retriever selection remain deferred until a domain corpus exists.
