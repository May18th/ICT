import json

from decision_poc import visualize
from decision_poc.scene_export import build_scene_payload
from decision_poc.web_demo import build_demo_payload, build_html, render_snapshot, route


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
    # 评审要看"谁触发了渲染"，所以面板在页面上
    assert "工具调用日志（谁触发了渲染）" in html


# --- P10：路由（纯函数，不起 socket） -------------------------------------


def test_pages_and_existing_routes_still_serve():
    assert route("/")[0] == 200
    assert route("/evolution")[0] == 200, "进化对比页是并行会话的产出，不能被路由改造丢掉"
    assert route("/api/evolution")[0] == 200
    assert route("/api/demo")[0] == 200


def test_unknown_route_is_404():
    status, content_type, body = route("/api/nope")
    assert status == 404
    assert "json" in content_type
    assert json.loads(body)["status"] == "error"


def test_scene_endpoints_and_compare(monkeypatch, tmp_path):
    monkeypatch.setenv("URBAN_TWIN_VISUAL_ROOT", str(tmp_path))

    # 还没有作业时给 404，而不是空对象（空对象会被前端当成"渲染成功但没内容"）
    assert route("/api/scene/latest")[0] == 404

    v1 = render_snapshot("v1")
    v4 = render_snapshot("v4")
    assert v1["snapshot_id"] != v4["snapshot_id"]

    status, _, body = route("/api/scene/" + v1["snapshot_id"])
    job = json.loads(body)
    assert status == 200
    assert job["snapshot_id"] == v1["snapshot_id"]
    assert job["scene"]["steps"]

    # UE 侧认领后 delivery 才能变 acked
    assert visualize.delivery_state(v1["snapshot_id"]) == "pending"
    assert route("/api/scene/" + v1["snapshot_id"] + "/ack", "POST")[0] == 200
    assert visualize.delivery_state(v1["snapshot_id"]) == "acked"

    status, _, body = route(
        f"/api/compare?baseline={v1['snapshot_id']}&candidate={v4['snapshot_id']}"
    )
    result = json.loads(body)
    assert status == 200
    assert result["verdict"]
    assert result["allocation"]["per_district"]

    # 日志必须能分辨"人点的"和"智能体调的"
    status, _, body = route("/api/tool-log?limit=10")
    entries = json.loads(body)["entries"]
    assert len(entries) >= 2
    assert all(entry["origin"] == "manual" for entry in entries)
    assert {entry["tool"] for entry in entries} == {"scene_render"}


def test_compare_requires_both_ids():
    status, _, body = route("/api/compare?baseline=a")
    assert status == 400
    assert json.loads(body)["status"] == "error"


def test_page_script_parses(tmp_path):
    """页面脚本必须能解析。

    这条是为一个真实踩过的坑加的：build_html() 是普通 Python 字符串，JS 里写 "\\n"
    会被解释成真实换行，把字符串字面量截断——整个 <script> 解析失败，页面按钮全哑。
    node 不在的环境跳过（不引入新依赖）。
    """
    import re
    import shutil
    import subprocess

    node = shutil.which("node")
    if not node:
        import pytest

        pytest.skip("node 未安装，跳过 JS 语法检查")

    blocks = re.findall(r"<script>(.*?)</script>", build_html(), re.S)
    assert blocks, "页面必须有脚本块"
    target = tmp_path / "page.js"
    target.write_text("\n".join(blocks), encoding="utf-8")
    completed = subprocess.run(
        [node, "--check", str(target)], capture_output=True, text=True, timeout=60
    )
    assert completed.returncode == 0, completed.stderr
