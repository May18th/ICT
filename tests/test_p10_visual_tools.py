"""P10 可视化工具层：快照 → 渲染作业 → 对比，以及三个 MCP 工具。

这些测试锁的是"决策能触发渲染"这件事本身，不是渲染好不好看：

- 同一决策同一 id（可核对"渲的是不是刚才那份结论"）；
- 换策略换 id，且对比能读出分配变化（P9 的进化可视化出口）；
- 渲染是异步的：作业落盘即返回，引擎认领与否只体现为 delivery，不算失败。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from decision_poc import visualize
from decision_poc.mcp_server import TOOL_GROUPS, McpServer, UrbanToolkit

EVOLVED_POLICY = {
    "policy_version": "v4",
    "weight_closure": 1.0,
    "weight_hazard": 0.25,
    "transfer_penalty": 0.1,
}


@pytest.fixture(scope="module")
def toolkit(tmp_path_factory) -> UrbanToolkit:
    root = tmp_path_factory.mktemp("visual")
    return UrbanToolkit(visual_root=str(root))


@pytest.fixture(scope="module")
def server(toolkit: UrbanToolkit) -> McpServer:
    return McpServer(toolkit)


@pytest.fixture(scope="module")
def baseline(server: McpServer) -> dict:
    payload = _call(server, "scene_render", {"policy": {"policy_version": "v1"}})
    assert payload["status"] == "ok", payload
    return payload["data"]


@pytest.fixture(scope="module")
def candidate(server: McpServer) -> dict:
    payload = _call(server, "scene_render", {"policy": EVOLVED_POLICY})
    assert payload["status"] == "ok", payload
    return payload["data"]


def _call(server: McpServer, name: str, arguments: dict, request_id: int = 101) -> dict:
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert "error" not in response, response.get("error")
    return json.loads(response["result"]["content"][0]["text"])


# --- 快照 ---------------------------------------------------------------


def test_snapshot_id_is_stable_and_policy_sensitive(
    server: McpServer, baseline: dict, candidate: dict
) -> None:
    again = _call(server, "scene_render", {"policy": {"policy_version": "v1"}})
    # 同一策略重跑 → 同 id（"渲的是不是刚才那份结论"可以当场核对）
    assert again["data"]["snapshot_id"] == baseline["snapshot_id"]
    # 换策略 → 换 id，否则对比就没有对象
    assert candidate["snapshot_id"] != baseline["snapshot_id"]


def test_snapshot_scenario_comes_from_layout(toolkit: UrbanToolkit) -> None:
    """A-03 同时有封路与施工段，取 construction（四套预设里的超集）。"""
    snapshot = visualize.build_visual_snapshot(runtime=toolkit._skill_runtime())
    assert snapshot.scenario == "construction"
    assert snapshot.steps, "六跳必须都在快照里"
    assert snapshot.allocation is not None, "有决策输入就必须给分配方案"
    assert snapshot.warnings == []


def test_snapshot_persists_and_reloads(toolkit: UrbanToolkit) -> None:
    snapshot = visualize.build_visual_snapshot(runtime=toolkit._skill_runtime())
    path = visualize.save_snapshot(snapshot, toolkit.visual_root)
    assert Path(path).is_file()
    loaded = visualize.load_snapshot(snapshot.snapshot_id, toolkit.visual_root)
    assert loaded.snapshot_id == snapshot.snapshot_id
    assert loaded.scene_hash == snapshot.scene_hash


def test_render_job_shape(toolkit: UrbanToolkit) -> None:
    snapshot = visualize.build_visual_snapshot(runtime=toolkit._skill_runtime())
    job = visualize.build_render_job(snapshot)
    assert job.schema_version == visualize.JOB_SCHEMA
    assert job.scene["scenario"] == snapshot.scenario
    assert job.scene["scene_hash"] == snapshot.scene_hash
    assert job.scene["steps"], "UE 按 steps 逐跳演"
    # 面板要带分配方案：策略差异是演给人看的那一层
    assert job.panel["allocation_lines"]
    assert snapshot.event_id in job.panel["text"]


def test_compare_reads_policy_effect(toolkit: UrbanToolkit) -> None:
    baseline = visualize.build_visual_snapshot(
        policy={"policy_version": "v1"}, runtime=toolkit._skill_runtime()
    )
    candidate = visualize.build_visual_snapshot(
        policy=EVOLVED_POLICY, runtime=toolkit._skill_runtime()
    )
    result = visualize.compare_snapshots(baseline, candidate)
    assert result.same_snapshot is False
    assert set(result.policy["changed"]) >= {"weight_closure", "transfer_penalty"}
    before = baseline.allocation["allocation_by_district"]
    after = candidate.allocation["allocation_by_district"]
    assert before != after, "策略改了，分配必须跟着变，否则对比没有意义"
    assert after["A-03"] >= before["A-03"], "封路权重上去，关键片区应该更受保障"
    assert "分配变化" in result.verdict
    assert result.metrics["delta"]


def test_compare_same_snapshot_is_flagged(toolkit: UrbanToolkit) -> None:
    snapshot = visualize.build_visual_snapshot(runtime=toolkit._skill_runtime())
    result = visualize.compare_snapshots(snapshot, snapshot)
    assert result.same_snapshot is True
    assert all(item["delta"] == 0 for item in result.allocation["per_district"])


# --- 资产 ---------------------------------------------------------------


def test_asset_lookup_reports_manifest() -> None:
    data = visualize.asset_lookup("construction")
    assert data["scenario"] == "construction"
    assert data["glb"].endswith(".glb")
    assert data["glb_exists"] is True
    assert data["objects"]["building"] == 120
    assert "generate_urban_twin.py" in data["regenerate_command"]


def test_asset_lookup_rejects_unknown_scenario() -> None:
    with pytest.raises(KeyError):
        visualize.asset_lookup("tsunami")


# --- MCP 工具 -----------------------------------------------------------


def test_scene_render_returns_job_and_pending_delivery(baseline: dict) -> None:
    assert baseline["scenario"] in visualize.SCENARIOS
    assert baseline["step_count"] >= 6
    assert baseline["highlight_count"] > 0
    assert baseline["allocation"]["demand"] > 0
    # 引擎没认领就是 pending：本工具不持有渲染器连接，不谎报"已渲染"
    assert baseline["delivery"] == "pending"
    assert baseline["fetch_url"].startswith("/api/scene/")
    assert Path(baseline["job_path"]).is_file()


def test_ack_flips_delivery_to_acked(server: McpServer, toolkit: UrbanToolkit, baseline: dict) -> None:
    visualize.write_ack(baseline["snapshot_id"], root=toolkit.visual_root)
    payload = _call(server, "scene_render", {"snapshot_id": baseline["snapshot_id"]})
    assert payload["status"] == "ok"
    assert payload["data"]["delivery"] == "acked"


def test_scene_render_unknown_snapshot_is_not_found(server: McpServer) -> None:
    payload = _call(server, "scene_render", {"snapshot_id": "snap-does-not-exist"})
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "NOT_FOUND"


def test_scene_render_rejects_non_object_policy(server: McpServer) -> None:
    payload = _call(server, "scene_render", {"policy": "v4"})
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "VALIDATION_FAILED"


def test_scene_compare_tool(server: McpServer, baseline: dict, candidate: dict) -> None:
    payload = _call(
        server,
        "scene_compare",
        {"baseline_id": baseline["snapshot_id"], "candidate_id": candidate["snapshot_id"]},
    )
    assert payload["status"] == "ok"
    assert payload["data"]["baseline_id"] == baseline["snapshot_id"]
    assert payload["data"]["allocation"]["per_district"]
    assert payload["data"]["verdict"]


def test_scene_compare_unknown_id(server: McpServer, baseline: dict) -> None:
    payload = _call(
        server,
        "scene_compare",
        {"baseline_id": baseline["snapshot_id"], "candidate_id": "snap-nope"},
    )
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "NOT_FOUND"


def test_asset_lookup_tool(server: McpServer) -> None:
    payload = _call(server, "asset_lookup", {"scenario": "flood"})
    assert payload["status"] == "ok"
    assert payload["data"]["glb_exists"] is True
    assert payload["data"]["note"], "必须写清'只核对不生成'"


def test_asset_lookup_tool_unknown_scenario(server: McpServer) -> None:
    payload = _call(server, "asset_lookup", {"scenario": "tsunami"})
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "NOT_FOUND"
    assert "tsunami" in json.dumps(payload["data"], ensure_ascii=False)


def test_tool_groups_filter() -> None:
    toolkit = UrbanToolkit()
    decision = [tool["name"] for tool in toolkit.tools(["decision"])]
    presentation = [tool["name"] for tool in toolkit.tools(["presentation"])]
    bootstrap = [tool["name"] for tool in toolkit.tools(["bootstrap"])]
    assert decision == list(TOOL_GROUPS["decision"])
    # P11 加了只读的 scene_status（查 out 片状态），仍属 presentation，不进推演循环
    assert presentation == ["scene_render", "scene_status", "scene_compare"]
    assert bootstrap == ["asset_lookup"]
    # 推演循环里看不见渲染工具：这是分组的全部意义
    assert "scene_render" not in decision
    assert len(toolkit.tools()) == 15


def test_unknown_group_raises() -> None:
    toolkit = UrbanToolkit()
    with pytest.raises(KeyError):
        toolkit.tools(["nope"])
