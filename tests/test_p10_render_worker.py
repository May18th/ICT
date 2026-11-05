"""P11 渲染执行器：`RenderJob` → 真成片 → ack。

锁的是"平台能拿到片子"这件事，不是片子好不好看：

- `asset` 模式端到端：scene_render 落盘作业 → worker 出片 → delivery 由 pending 变 acked；
- 幂等：同一 snapshot 再调一次直接 reused，不重渲；
- ack 里的 sha256 与磁盘上的片子对得上（"平台拿到的是那一份"可当场核对）；
- ack 不含位姿/材质/镜头——渲染执行器也不许把这些漏出去；
- Blender 环境缺失时**明确失败**，不静默降级成假渲染。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from decision_poc import render_worker, visualize
from decision_poc.mcp_server import McpServer, UrbanToolkit

FORBIDDEN_KEYS = {"camera", "lens", "material", "materials", "position", "rotation", "scale", "actor"}


@pytest.fixture(scope="module")
def toolkit(tmp_path_factory) -> UrbanToolkit:
    return UrbanToolkit(visual_root=str(tmp_path_factory.mktemp("render")))


@pytest.fixture(scope="module")
def server(toolkit: UrbanToolkit) -> McpServer:
    return McpServer(toolkit)


def _call(server: McpServer, name: str, arguments: dict) -> dict:
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 201,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        }
    )
    assert "error" not in response, response.get("error")
    return json.loads(response["result"]["content"][0]["text"])


@pytest.fixture(scope="module")
def rendered(server: McpServer, toolkit: UrbanToolkit) -> tuple[dict, render_worker.RenderOutcome]:
    payload = _call(server, "scene_render", {"policy": {"policy_version": "v1"}})
    assert payload["status"] == "ok", payload
    job = payload["data"]
    outcome = render_worker.render(job["snapshot_id"], mode="asset", root=toolkit.visual_root)
    return job, outcome


def test_job_carries_event_id(rendered, toolkit: UrbanToolkit) -> None:
    job, _ = rendered
    loaded = visualize.load_job(job["snapshot_id"], toolkit.visual_root)
    assert loaded.scene["event_id"], "作业必须自述演的是哪个工单，否则资产无从匹配"


def test_asset_mode_acks_with_real_clip(rendered, toolkit: UrbanToolkit) -> None:
    job, outcome = rendered
    assert outcome.status == "acked", outcome.detail
    art = outcome.artifact
    assert art["kind"] == "video/mp4"
    assert art["bytes"] > 100_000
    clip = Path(toolkit.visual_root) / art["path"]
    assert clip.is_file()
    digest = hashlib.sha256(clip.read_bytes()).hexdigest()
    assert digest == art["sha256"]
    assert visualize.delivery_state(job["snapshot_id"], toolkit.visual_root) == "acked"


def test_second_call_is_idempotent(rendered, toolkit: UrbanToolkit) -> None:
    job, first = rendered
    again = render_worker.render(job["snapshot_id"], mode="asset", root=toolkit.visual_root)
    assert again.status == "reused"
    assert again.artifact["sha256"] == first.artifact["sha256"]


def test_ack_artifact_has_no_low_level_params(rendered, toolkit: UrbanToolkit) -> None:
    job, _ = rendered
    ack = visualize.read_ack(job["snapshot_id"], toolkit.visual_root)
    assert ack is not None
    flat = json.dumps(ack, ensure_ascii=False).lower()
    for key in FORBIDDEN_KEYS:
        assert f'"{key}"' not in flat, f"ack 里不该出现渲染控制参数 {key}"


def test_scene_status_reports_pending_then_acked(server: McpServer, toolkit: UrbanToolkit) -> None:
    payload = _call(server, "scene_render", {"policy": {"policy_version": "v2"}})
    snapshot_id = payload["data"]["snapshot_id"]

    pending = _call(server, "scene_status", {"snapshot_id": snapshot_id})
    assert pending["status"] == "ok"
    assert pending["data"]["delivery"] == "pending"
    assert pending["data"]["artifact"] == {}
    assert "render_worker" in pending["data"]["next_action"]

    render_worker.render(snapshot_id, mode="asset", root=toolkit.visual_root)
    acked = _call(server, "scene_status", {"snapshot_id": snapshot_id})
    assert acked["data"]["delivery"] == "acked"
    assert acked["data"]["artifact"]["sha256"]
    assert acked["data"]["engine"] == "blender-asset"


def test_scene_status_unknown_snapshot(server: McpServer) -> None:
    payload = _call(server, "scene_status", {"snapshot_id": "snap-nope"})
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "NOT_FOUND"


def test_blender_mode_without_env_fails_loudly(rendered, toolkit: UrbanToolkit, monkeypatch) -> None:
    job, _ = rendered
    for key in ("DECISION_POC_BLENDER_EXE", "DECISION_POC_BLENDER_SCENE", "DECISION_POC_BLENDER_SCRIPT"):
        monkeypatch.delenv(key, raising=False)
    outcome = render_worker.render(
        job["snapshot_id"], mode="blender", root=toolkit.visual_root, force=True
    )
    assert outcome.status == "failed"
    assert "DECISION_POC_BLENDER_EXE" in outcome.detail
    # 失败不写 ack：pending 是诚实的，不能因为跑过就报"已渲染"
    assert visualize.delivery_state(job["snapshot_id"], toolkit.visual_root) == "acked"  # 上一次的 ack 仍在
    assert outcome.artifact == {}
