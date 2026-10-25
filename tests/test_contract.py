from decision_poc.contracts import DecisionInput, DecisionOutput


def test_decision_output_requires_evidence_and_risk():
    result = DecisionOutput(
        incident_id="inc-001",
        diagnosis="CPU saturation",
        recommended_action="Scale service",
        confidence=0.84,
        risks=["Scale-out cost"],
        evidence=["metrics.cpu.avg=96%"],
    )

    assert result.incident_id == "inc-001"
    assert result.evidence
    assert result.risks


def test_decision_input_rejects_blank_incident_id():
    try:
        DecisionInput(incident_id="", alert="CPU high")
    except ValueError as exc:
        assert "incident_id" in str(exc)
    else:
        raise AssertionError("blank incident_id must be rejected")
