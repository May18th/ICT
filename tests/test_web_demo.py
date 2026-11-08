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
    assert "受影响对象" in html
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


def test_page_has_scheduler_panels_and_live_scene():
    """微调度台的六块面板要有容器、有取数函数、场景图不再硬编码。

    这条防的是"面板加了但没接数据"：容器在、函数不在，页面看起来正常，点开一片空白。
    """
    from decision_poc.web_demo import build_html

    html = build_html()
    for marker in (
        "演示链路",
        "工单列表",
        "资源状态",
        "当前方案 vs 备选方案",
        "微调度台 · 决策事件流",
        "id=scenemap",
        "id=orders",
        "id=resboard",
        "id=plans",
        "id=chain",
    ):
        assert marker in html, f"页面缺 {marker}"
    for fn in ("loadScheduler", "drawScene", "pickEntity", "renderOrders", "renderResources", "renderPlan"):
        assert f"function {fn}" in html, f"页面缺取数函数 {fn}"
    # 场景实体要能键盘操作：role=button + tabindex 只让元素"能聚焦"，
    # 实测按 Enter 详情面板纹丝不动（before=0 / after=0），必须显式挂 onkeydown。
    assert "onkeydown=\"entKey(event," in html
    assert "function entKey" in html and "preventDefault" in html
    assert "fetch('/api/scheduler')" in html
    assert "受影响对象" in html
    assert "<span>事件</span><b>→</b><span>受影响对象</span><b>→</b><span>处置动作</span>" in html
    assert 'id=scene-summary' in html
    assert 'class=page-hero' in html
    assert 'outcome-card' in html
    assert 'process-card' in html
    assert 'primary-grid' in html
    assert 'data-grid' in html
    assert 'id=scene-detail' in html
    assert 'focusable="true"' in html
    assert '.ivbtn{' in html and 'white-space:normal' in html
    assert "查看判断依据" in html
    # 场景图必须是按实体渲染的，不能退回固定椭圆
    assert "polygon class=" in html
    assert "<ellipse class=flood" not in html


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


def test_flood_playback_is_event_driven():
    """动态内涝推演的时序必须由事件流驱动，不许退回硬编码节拍。

    真实事故：playFloodScenario 里节拍、水位、阶段文案全是写死的——650ms 一拍、
    水位线性涨到 30 cm、六个阶段标题是字符串常量，事件流的 29 条数据在页面上
    只贡献"事件 29 条"一个数字。评委若问"涨水节奏根据什么算的"就答不出来。
    """
    html = build_html()
    assert "filter(e=>e.kind==='stage')" in html, "播放必须从事件流取 stage 事件驱动"
    assert "e.title||e.phase" in html, "阶段文案必须来自事件本身"
    assert "降雨与屋面汇流" not in html, "硬编码阶段文案又回来了"
    assert "maxDepth" in html and "depth_cm" in html, \
        "水位终值必须取实测（观测事件 detail 或 facts.depth_cm），不允许只写死 30"
    assert "id=flood-cap-title" in html and "id=flood-cap-detail" in html
    assert "document.getElementById('flood-cap-detail').textContent=deptEl.textContent" in html
    assert "id=flood-gap" in html
    assert "class=flood-gap-value" in html
    assert ".flood-gap-value{font-size:28px" in html
    run_body = html.split("async function run(isSwitch)", 1)[1].split("\n let lastV1", 1)[0]
    assert "playFloodScenario()" not in run_body
    assert "setTimeout(function(){runChain()" not in html
    assert "loadScheduler().then(function(){chainAll(true)})" in html
    assert "flood-story" in html and "flood-cause" in html
    assert "twin-reading-guide" in html and "对象 → 影响 → 动作" in html
    # 受影响对象卡已从「第二张地图」改为对象清单（上方动画地图不再有雷同副本），
    # scene-step 编号随旧地图删除；清单容器 ent-item + 详情面板 scene-action 必须在。
    assert "ent-item" in html and "scene-action" in html
    assert "id=scenemap class=entlist" in html
