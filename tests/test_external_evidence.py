from decision_poc.external_evidence import external_items_to_tool_result


def test_external_evidence_maps_citations_to_existing_tool_envelope():
    result = external_items_to_tool_result(
        request_id="req-1",
        items=[
            {
                "source": "ISO 10816-3:2009",
                "content": "7.8 mm/s is in D zone",
                "version": "2009",
                "locator": "table 2",
            }
        ],
    )

    assert result.status == "ok"
    assert result.data["items"][0]["source"] == "ISO 10816-3:2009"
    assert result.data["items"][0]["locator"] == "table 2"
    assert result.context.source == "external://evidence/req-1"


def test_external_evidence_rejects_missing_citation_fields():
    try:
        external_items_to_tool_result(request_id="req-1", items=[{"content": "x"}])
    except ValueError as exc:
        assert "source" in str(exc)
    else:
        raise AssertionError("expected ValueError")
