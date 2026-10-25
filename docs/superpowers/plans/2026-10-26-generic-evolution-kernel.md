# Generic Evolution Kernel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extend the isolated PoC into a domain-neutral, testable decision kernel with simulated tools and approvals, feedback-driven candidate versions, evaluation-gated promotion, and rollback.

**Architecture:** LangGraph remains the sole workflow orchestrator and carries only JSON-serializable state. Pydantic AI remains the typed decision node; deterministic policy, approval, tool-envelope, and version-gating logic stay in ordinary Python. LangChain Core stays at the demonstration/evaluation boundary and does not create a second agent loop.

**Tech Stack:** Python 3.12, LangGraph 1.2.17, Pydantic AI Slim 2.61.0, Pydantic 2.14.1, LangChain Core 1.7.0, pytest 9.1.1.

**Spec:** User-confirmed scope in the conversation dated 2026-10-26: generic decision schema/state flow; simulated data, tools, and approval; retries, validation, feedback, version rollback; record technical risks and performance baseline; verify controlled evolution; defer industry-specific logic, production integrations, complete UI, and final domain metrics.

## Global Constraints

- Keep schemas and workflow domain-neutral; do not add industry-specific fields, rules, tools, or scenarios.
- Do not connect Nexent, real models, production systems, credentials, or real side-effecting tools.
- LangGraph alone owns the workflow state transitions; Pydantic AI performs typed decision generation; LangChain Core is only a demo/evaluation adapter.
- Mock tools return a structured `status/data/error/stats/context` envelope and have bounded retry behavior.
- Approval is simulated and can block execution; no UI or real identity/authorization system is added.
- Evaluation accepts caller-supplied reports/metrics; do not freeze domain-specific score thresholds.
- Keep Python and direct dependency pins as declared in `pyproject.toml`; update `uv.lock` only through `uv lock`/`uv sync`.
- Do not include credentials, `.venv`, caches, or local runtime artifacts in Git.

## Review Focus

- Malformed or blank generic request fields must fail schema validation before model/tool work (`tests/test_contract.py`).
- Transient tool failure must retry only within the configured bound; permanent failure must stop with an explicit structured error (`tests/test_tools.py`).
- A decision with the wrong correlation ID or missing evidence must not reach approval/execution (`tests/test_graph.py`).
- Rejected or absent approval must block simulated execution (`tests/test_graph.py`).
- Failed evaluation must leave the active policy unchanged; explicit rollback must restore a previously accepted version (`tests/test_evolution.py`).

---

### Task 1: Replace the incident-shaped contract with generic contracts and mock tools

**Files:**
- Modify: `src/decision_poc/contracts.py`
- Create: `src/decision_poc/tools.py`
- Modify: `src/decision_poc/__init__.py`
- Modify: `tests/test_contract.py`
- Create: `tests/test_tools.py`

**Interfaces:**
- `DecisionRequest(request_id: str, goal: str, context: dict[str, Any], constraints: list[str])`
- `EvidenceItem(source: str, content: str, version: str | None)`
- `DecisionOption(option_id: str, description: str, expected_impact: str)`
- `DecisionOutput(request_id, summary, options, selected_option_id, confidence, risk_level, risks, evidence)` with `RiskLevel` values `low`, `medium`, `high`.
- `ToolResult(status, data, error, stats, context)` with explicit `ToolError(code, message, retryable)`, attempt/latency stats, and source/version context.
- `SimulatedEvidenceTool.run(request, scripted_results) -> ToolResult` retries retryable outcomes only up to `max_attempts`; it performs no external I/O.

- [x] **Step 1: Write contract and tool tests first**
  - Replace incident-specific tests with generic request validation and decision correlation/evidence schema tests.
  - Add `test_transient_tool_error_retries_until_success`, `test_tool_retries_are_bounded`, and `test_non_retryable_tool_error_is_not_retried`.
- [x] **Step 2: Run the focused tests and verify expected failures**

```powershell
uv run --locked pytest tests/test_contract.py tests/test_tools.py -q
```

Expected: collection or assertion failures for the not-yet-defined generic contracts/tool behavior.

- [x] **Step 3: Implement generic Pydantic models and the deterministic mock tool**
  - Keep unknown input fields forbidden.
  - Validate nonblank identifiers and text, confidence in `[0, 1]`, selected option membership, and nonempty evidence for a usable decision.
  - Preserve the final structured error and attempt count when retries are exhausted.
- [x] **Step 4: Run the focused tests; defer full-suite verification until Task 3 migrates graph consumers**

```powershell
uv run --locked pytest tests/test_contract.py tests/test_tools.py -q
```

Expected: all focused tests pass. The pre-existing graph/demo suite intentionally cannot collect until Task 3 replaces its incident-shaped API; this temporary incompatibility is recorded in the execution ledger and must be cleared by Task 3.

### Task 2: Implement feedback and evaluation-gated policy versioning

**Files:**
- Create: `src/decision_poc/evolution.py`
- Create: `tests/test_evolution.py`
- Modify: `src/decision_poc/contracts.py`

**Interfaces:**
- `FeedbackRecord(request_id, outcome, note, proposed_updates)`; `outcome` is generic positive/negative/neutral feedback.
- `PolicyVersion(version_id, parent_version_id, parameters)`.
- `EvaluationReport(passed, metrics, notes)` where `metrics` is an open numeric/string map, not a fixed business scorecard.
- `VersionRegistry.propose(feedback) -> PolicyVersion` creates an immutable candidate from the active version and supplied generic updates.
- `VersionRegistry.promote(candidate, report) -> PolicyVersion` changes active version only when `report.passed` is true.
- `VersionRegistry.rollback(version_id) -> PolicyVersion` only targets a known version and records the rollback in history.

- [x] **Step 1: Write version lifecycle tests first**
  - Add `test_feedback_creates_candidate_without_mutating_active_version`.
  - Add `test_failed_evaluation_keeps_active_version_unchanged`.
  - Add `test_passed_evaluation_promotes_candidate`.
  - Add `test_rollback_restores_known_previous_version_and_unknown_version_fails`.
- [x] **Step 2: Run the focused tests and verify the missing behavior**

```powershell
uv run --locked pytest tests/test_evolution.py -q
```

Expected: failures because the registry and version contracts do not yet exist.

- [x] **Step 3: Implement immutable candidate creation, evaluation gate, and rollback**
  - Candidate creation is a pure copy-and-merge of generic policy parameters.
  - A failed report must not alter active version or history of accepted versions.
  - A rollback must preserve the rejected/current version in history and activate only an existing accepted target.
- [x] **Step 4: Run evolution tests and the compatible focused suite; defer full-suite verification until Task 3**

```powershell
uv run --locked pytest tests/test_evolution.py -q
uv run --locked pytest tests/test_contract.py tests/test_tools.py tests/test_evolution.py -q
```

Expected: all listed tests pass. The legacy graph/demo tests remain temporarily incompatible until Task 3 migrates their incident-shaped API, as recorded in the ledger.

### Task 3: Extend LangGraph into a simulated decision, approval, and feedback flow

**Files:**
- Modify: `src/decision_poc/graph.py`
- Modify: `src/decision_poc/agent.py`
- Modify: `src/decision_poc/contracts.py`
- Modify: `src/decision_poc/tools.py`
- Modify: `tests/test_decision_flow.py`

**Interfaces:**
- `run_decision_async(request, *, model_output, evidence_results, approval, feedback, evaluation_report) -> DecisionState`.
- State includes JSON-compatible `request`, evidence/tool envelopes, decision, approval status, simulated execution result, feedback, candidate/active version summaries, trace, retry counts, and `termination_reason`.
- Graph nodes execute in this order: collect evidence → generate typed decision → validate correlation/evidence → route simulated approval → simulate execution only if approved → record feedback → propose candidate → evaluate and promote/retain active version.
- A single `DecisionPolicy` supplies `max_tool_attempts` and required-approval risk levels; no industry behavior is encoded.

- [x] **Step 1: Write graph branch and serialization tests first**
  - Add `test_approved_request_reaches_simulated_execution_and_feedback`.
  - Add `test_rejected_approval_blocks_execution` and `test_missing_approval_blocks_high_risk_execution`.
  - Add `test_invalid_decision_stops_before_approval`.
  - Add `test_graph_state_is_json_serializable_after_success_and_tool_failure`.
  - Preserve LangChain sync and async Runnable boundary tests using the new generic input.
- [x] **Step 2: Run focused tests and verify expected failures**

```powershell
uv run --locked pytest tests/test_graph.py tests/test_decision_flow.py -q
```

Expected: failures for the not-yet-implemented graph branches and generic API.

- [x] **Step 3: Implement graph state/nodes and explicit routing**
  - Store only JSON-compatible values in LangGraph state; validate with Pydantic at node boundaries.
  - Stop before execution on evidence/decision validation errors or denied/missing required approval.
  - Use simulated tool envelopes and bounded retries; retain error, attempt count, and trace.
  - Invoke the version registry after feedback; report whether the candidate was promoted or retained as unevaluated/rejected, without inventing a domain score.
- [x] **Step 4: Run graph tests and the full suite**

```powershell
uv run --locked pytest tests/test_graph.py tests/test_decision_flow.py -q
uv run --locked pytest -q
```

Expected: all success, rejection, failure, and serialization cases pass.

### Task 4: Keep LangChain at the boundary and document reproducible technical baselines

**Files:**
- Modify: `src/decision_poc/demo.py`
- Create: `src/decision_poc/evaluation.py`
- Create: `tests/test_evaluation.py`
- Modify: `README.md`

**Interfaces:**
- `build_demo_runnable(...)` adapts generic request input to the LangGraph flow and returns JSON-safe output.
- `evaluate_cases(runnable, cases) -> EvaluationSummary` records technical outcomes: completed/blocked/error counts, graph steps, retry totals, serialization status, and elapsed time for observation only.
- Evaluation cases can supply arbitrary assertions/expectations; no fixed final competition or industry thresholds are introduced.

- [x] **Step 1: Write boundary and baseline tests first**
  - Add `test_langchain_adapter_calls_graph_and_returns_json_safe_output`.
  - Add `test_evaluation_summary_records_generic_technical_baselines`.
  - Add `test_evaluation_does_not_claim_promotion_when_case_fails`.
- [x] **Step 2: Run focused tests and verify missing behavior**

```powershell
uv run --locked pytest tests/test_decision_flow.py tests/test_evaluation.py -q
```

Expected: failures for the missing generic boundary and evaluation summary.

- [x] **Step 3: Implement thin LangChain adapter, technical evaluator, and docs**
  - Keep business flow out of `demo.py` and evaluation helpers.
  - Document architecture boundaries, local run commands, baseline fields, non-goals, and known limits.
  - Do not add any new dependency unless a test proves an essential capability is missing.
- [x] **Step 4: Run final verification and inspect staged changes**

```powershell
uv lock --check
uv run --locked python -m compileall -q src tests
uv run --locked pytest -q
uv pip check --python .venv\Scripts\python.exe
git status --short --ignored
```

Expected: lock is current, compilation succeeds, all tests pass, dependencies are compatible, and `.venv`/caches remain ignored.
