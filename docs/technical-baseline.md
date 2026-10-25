# Technical Baseline

`decision_poc.baseline.run_technical_baseline()` runs three domain-neutral paths:

1. a completed low-risk decision with simulated evidence and approval;
2. a high-risk decision blocked by missing approval;
3. a decision blocked by missing simulated evidence.

The baseline records completed, blocked, and error counts; graph steps; retry counts; JSON serialization; and elapsed time. It does not claim that one path is better for any industry, and it does not freeze competition metrics.

Run it with:

```powershell
$env:PYTHONPATH = "src"
uv run --locked python -c "from decision_poc.baseline import run_technical_baseline; print(run_technical_baseline())"
```

The scenarios are intentionally generic and local. They do not connect to Nexent, an LLM provider, a database, or a side-effecting tool.

## Recorded risks

- The current version registry is in-memory; process restart loses candidate and history state.
- The Pydantic AI model is `TestModel`; real provider latency, quality, and failure modes are not measured.
- Simulated execution has no external side effect, so permission, idempotency, and rollback of a real action remain unverified.
- LangGraph checkpoint persistence and interruption/resume are not yet configured.
- No final industry metric is defined until the competition scenario is selected.
