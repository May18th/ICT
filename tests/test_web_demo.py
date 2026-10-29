import json
from decision_poc.web_demo import build_demo_payload, build_html
from decision_poc.scene_export import build_scene_payload


def test_demo_payload_contains_event_and_trace():
    payload = build_demo_payload()
    assert payload["event_id"] == "WO-20261029-007"
    assert payload["status"] == "ok"
    assert payload["steps"]
    assert payload["evidence"]


def test_scene_payload_is_engine_neutral():
    payload = build_scene_payload()
    assert payload["schema_version"] == "urban-twin-scene-v1"
    assert payload["objects"]
    assert all("twin_id" in item and "position" in item for item in payload["objects"])
    json.dumps(payload, ensure_ascii=False)


def test_html_contains_demo_panels_and_api_route():
    html = build_html()
    assert "数字孪生影响范围" in html
    assert "跨部门协同" in html
    assert "fetch('/api/demo')" in html
