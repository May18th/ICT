import asyncio
import json
import pytest

from decision_poc.demo import build_demo_runnable
from decision_poc.graph import run_decision_async
from decision_poc.contracts import DecisionInput, DecisionOutput


def test_langgraph_flow_uses_pydantic_ai_output_and_finishes():
    request = DecisionInput(
        incident_id="inc-001",
        alert="CPU usage is above 95% for five minutes",
        service="orders-api",
    )
    model_output = {
        "incident_id": "inc-001",
        "diagnosis": "orders-api is CPU saturated",
        "recommended_action": "Scale the service by one replica",
        "confidence": 0.88,
        "risks": ["Additional compute cost"],
        "evidence": ["metrics.cpu.avg=96%", "duration=5m"],
    }

    result = asyncio.run(run_decision_async(request, model_output=model_output))

    decision = DecisionOutput.model_validate(result["decision"])
    assert decision.recommended_action.startswith("Scale")
    assert result["termination_reason"] == "completed"
    assert result["trace"] == ["decision_generated", "decision_validated"]


def test_langchain_demo_runnable_returns_a_serializable_result():
    runnable = build_demo_runnable(
        model_output={
            "incident_id": "inc-002",
            "diagnosis": "database latency",
            "recommended_action": "Inspect the slow query dashboard",
            "confidence": 0.71,
            "risks": ["Investigation delay"],
            "evidence": ["metrics.db.p95=1800ms"],
        }
    )

    result = asyncio.run(
        runnable.ainvoke(
            {
                "incident_id": "inc-002",
                "alert": "Database p95 latency is high",
                "service": "orders-api",
            }
        )
    )

    assert result["termination_reason"] == "completed"
    assert result["decision"]["incident_id"] == "inc-002"


def test_decision_flow_rejects_model_output_for_another_incident():
    request = DecisionInput(
        incident_id="inc-003",
        alert="Queue depth is increasing",
        service="worker",
    )
    wrong_incident_output = {
        "incident_id": "inc-other",
        "diagnosis": "queue backlog",
        "recommended_action": "Inspect worker capacity",
        "confidence": 0.70,
        "risks": ["Delayed processing"],
        "evidence": ["queue.depth=500"],
    }

    with pytest.raises(ValueError, match="incident_id"):
        asyncio.run(
            run_decision_async(request, model_output=wrong_incident_output)
        )


def test_async_graph_state_is_json_serializable():
    request = DecisionInput(
        incident_id="inc-004",
        alert="Storage latency increased",
        service="object-store",
    )
    model_output = {
        "incident_id": "inc-004",
        "diagnosis": "Storage latency increased",
        "recommended_action": "Inspect storage node metrics",
        "confidence": 0.68,
        "risks": ["The cause may remain unresolved"],
        "evidence": ["metrics.storage.p95=900ms"],
    }

    result = asyncio.run(run_decision_async(request, model_output=model_output))

    encoded = json.dumps(result, ensure_ascii=False)
    assert json.loads(encoded)["decision"]["incident_id"] == "inc-004"
