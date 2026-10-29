from decision_poc.contracts import DecisionRequest
from decision_poc.nexent_adapter import NexentAdapter


def test_adapter_wraps_decision_for_nexent_without_network_side_effects():
    request = DecisionRequest(
        request_id="req-1",
        goal="choose",
        context={"team": "ict"},
        constraints=["safe"],
    )
    calls = []

    def invoke(payload):
        calls.append(payload)
        return {"status": "accepted", "run_id": "run-1"}

    result = NexentAdapter(invoke=invoke).run(request)

    assert result == {"status": "accepted", "run_id": "run-1"}
    assert calls == [
        {
            "request_id": "req-1",
            "goal": "choose",
            "context": {"team": "ict"},
            "constraints": ["safe"],
        }
    ]


def test_adapter_rejects_non_mapping_transport_response():
    request = DecisionRequest(
        request_id="req-1", goal="choose", context={}, constraints=[]
    )

    def invoke(_payload):
        return "accepted"

    try:
        NexentAdapter(invoke=invoke).run(request)
    except TypeError as exc:
        assert "mapping" in str(exc)
    else:
        raise AssertionError("expected TypeError")

