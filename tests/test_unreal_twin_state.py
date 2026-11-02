"""UE 侧脚本的契约测试：把 `unreal` 模块打桩，验证它到底怎么调引擎。

为什么值得这么测：`urban_twin_state.py` 只能跑在 UE 里，没有引擎就完全测不到。
但"它是否按契约调用引擎 API"是可以离线验证的——把 `unreal` 当外部依赖注入一个
记录调用的假模块，就能断言：

- `apply_job` 按作业的 `scenario` 切了场景状态（封路变红 / 水面显隐）；
- `show_step` 把该跳的 `twin_id` 换成了高亮材质；
- `panel.text` 真的写到了面板；
- `sync_from_files` 取的是**最新**作业，并写回 ack（`scene_render` 的 delivery 读它）。

这些断言不能代替"在编辑器里跑一遍"，但能把"字段名对不上""忘了写 ack"这类
环境无关的错误在上机前挡掉。
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "visualization" / "unreal" / "Content" / "Python" / "urban_twin_state.py"


class _Material:
    def __init__(self, path: str) -> None:
        self.path = path


class _Vector:
    def __init__(self, x=0.0, y=0.0, z=0.0) -> None:
        self.x, self.y, self.z = x, y, z


class _Rotator:
    def __init__(self, pitch=0.0, yaw=0.0, roll=0.0) -> None:
        self.pitch, self.yaw, self.roll = pitch, yaw, roll


class _Text:
    def __init__(self, value: str) -> None:
        self.value = value


class _Component:
    def __init__(self, owner) -> None:
        self.owner = owner

    def set_material(self, index: int, material) -> None:
        self.owner.calls.append(("set_material", getattr(material, "path", None), index))

    def set_text(self, text) -> None:
        self.owner.calls.append(("set_text", getattr(text, "value", None)))

    def set_world_size(self, value) -> None:
        self.owner.calls.append(("set_world_size", value))


class _Actor:
    def __init__(self, name: str, tags: tuple[str, ...] = (), label: str | None = None) -> None:
        self.name = name
        self.tags = list(tags)
        self.label = label or name
        self.calls: list[tuple] = []
        self.hidden = None

    def get_editor_property(self, prop: str):
        assert prop == "tags", prop
        return list(self.tags)

    def get_actor_label(self) -> str:
        return self.label

    def set_actor_label(self, value: str) -> None:
        self.label = value

    def set_actor_hidden_in_game(self, value: bool) -> None:
        self.hidden = value

    def set_is_temporarily_hidden_in_editor(self, value: bool) -> None:
        self.calls.append(("hidden_editor", value))

    def get_component_by_class(self, cls):
        key = cls.__name__
        store = getattr(self, "_components", None)
        if store is None:
            store = self._components = {}
        if key not in store:
            store[key] = _Component(self)
        return store[key]


class _EditorActorSubsystem:
    level: list[_Actor] = []

    def get_all_level_actors(self) -> list[_Actor]:
        return list(_EditorActorSubsystem.level)

    def spawn_actor_from_class(self, klass, location, rotation):
        actor = _Actor(f"spawned_{klass.__name__}", label=f"spawned_{len(_EditorActorSubsystem.level)}")
        _EditorActorSubsystem.level.append(actor)
        return actor


class _EditorAssetLibrary:
    @staticmethod
    def load_asset(path: str):
        return _Material(path)


def _build_stub() -> tuple[types.ModuleType, dict]:
    state: dict = {"logs": [], "level": []}
    stub = types.ModuleType("unreal")

    class StaticMeshComponent:
        pass

    class TextRenderComponent:
        pass

    class TextRenderActor:
        pass

    class EditorActorSubsystem:
        _stub = _EditorActorSubsystem

    def get_editor_subsystem(cls):
        _EditorActorSubsystem.level = state["level"]
        return _EditorActorSubsystem()

    def log(message: str) -> None:
        state["logs"].append(message)

    class SystemLibrary:
        @staticmethod
        def get_command_line() -> str:
            return ""

    stub.StaticMeshComponent = StaticMeshComponent
    stub.TextRenderComponent = TextRenderComponent
    stub.TextRenderActor = TextRenderActor
    stub.EditorActorSubsystem = EditorActorSubsystem
    stub.EditorAssetLibrary = _EditorAssetLibrary
    stub.SystemLibrary = SystemLibrary
    stub.Text = _Text
    stub.Vector = _Vector
    stub.Rotator = _Rotator
    stub.get_editor_subsystem = get_editor_subsystem
    stub.log = log
    return stub, state


@pytest.fixture()
def twin():
    """每个测试一个干净的假引擎 + 重新加载一遍脚本（模块级 `main()` 会跑一次自检，无害）。"""
    stub, state = _build_stub()
    _EditorActorSubsystem.level = state["level"]
    sys.modules["unreal"] = stub
    for name in list(sys.modules):
        if name == "urban_twin_state":
            del sys.modules[name]
    spec = importlib.util.spec_from_file_location("urban_twin_state", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.STATE = state  # 测试里取日志用
    return module, state


def _level_actor(state: dict, name: str, tags: tuple[str, ...]) -> _Actor:
    actor = _Actor(name, tags=tags)
    state["level"].append(actor)
    return actor


# --- apply_job -----------------------------------------------------------


def test_apply_job_switches_scenario_highlights_and_panel(twin, tmp_path):
    module, state = twin
    water = _level_actor(state, "Water_WO", ("twin:TW-water-WO-20261029-007", "kind:water"))
    road_closed = _level_actor(
        state, "Road_R101", ("twin:TW-road-R-101", "kind:road", "state:closed")
    )
    road_normal = _level_actor(
        state, "Road_R102", ("twin:TW-road-R-102", "kind:road", "state:normal")
    )

    job = {
        "schema_version": "urban-twin-render-job-v1",
        "snapshot_id": "snap-test",
        "scene": {
            "scenario": "closure",
            "steps": [
                {
                    "index": 0,
                    "stage": "observed",
                    "title": "现象：积水与预警",
                    "status": "ok",
                    "highlights": ["TW-water-WO-20261029-007"],
                    "narrative": "A-03 积水 30cm",
                    "evidence": ["a#line:1"],
                }
            ],
            "actions": [],
        },
        "panel": {"text": "分配 6/7 台"},
    }

    module.apply_job(job)

    # scenario=closure → 水面显示 + 封路换封闭材质
    assert water.hidden is False
    assert ("set_material", "/Game/UrbanTwin/Materials/M_Road_Closed", 0) in road_closed.calls
    assert ("set_material", "/Game/UrbanTwin/Materials/M_Road_Normal", 0) in road_normal.calls
    # 第 0 跳高亮的是水面对象
    assert ("set_material", "/Game/UrbanTwin/Materials/M_Highlight", 0) in water.calls
    # 面板真的写了字（内容来自该跳，不是作业的 panel.text）
    texts = [item[1] for item in state["level"][-1].calls if item[0] == "set_text"]
    assert texts and "现象" in texts[0]


def test_apply_scenario_hides_water_when_normal(twin):
    module, state = twin
    water = _level_actor(state, "Water", ("twin:TW-water-X", "kind:water"))
    module.apply_scenario("normal")
    assert water.hidden is True


# --- 作业契约 -----------------------------------------------------------


@pytest.fixture(scope="module")
def toolkit():
    from decision_poc.mcp_server import UrbanToolkit

    return UrbanToolkit()


@pytest.fixture(scope="module")
def real_jobs(toolkit):
    """真实渲染作业：用 P10 的产物验证 UE 侧契约，而不是手写一个"我觉得对"的 JSON。"""
    from decision_poc import visualize

    v1 = visualize.build_visual_snapshot(
        policy={"policy_version": "v1"}, runtime=toolkit._skill_runtime()
    )
    v4 = visualize.build_visual_snapshot(
        policy={
            "policy_version": "v4",
            "weight_closure": 1.0,
            "weight_hazard": 0.25,
            "transfer_penalty": 0.1,
        },
        runtime=toolkit._skill_runtime(),
    )
    return v1, v4


def test_selftest_passes_on_real_job(twin, real_jobs, tmp_path):
    module, _ = twin
    from decision_poc import visualize

    snapshot, _unused = real_jobs
    path = visualize.save_job(visualize.build_render_job(snapshot), tmp_path)
    report = module.selftest_render_job(path)
    assert report["ok"] is True, report["problems"]
    assert report["steps"] >= 6


def test_selftest_reports_broken_job(twin, tmp_path):
    module, _ = twin
    broken = tmp_path / "broken.json"
    broken.write_text(
        json.dumps({"schema_version": "nope", "scene": {"scenario": "tsunami", "steps": []}}),
        encoding="utf-8",
    )
    report = module.selftest_render_job(broken)
    assert report["ok"] is False
    assert any("scenario" in problem for problem in report["problems"])
    assert any("steps" in problem for problem in report["problems"])


def test_sync_from_files_takes_newest_and_writes_ack(twin, real_jobs, tmp_path):
    module, state = twin
    from decision_poc import visualize

    old, new = real_jobs
    visualize.save_snapshot(old, tmp_path)
    visualize.save_snapshot(new, tmp_path)
    old_path = visualize.save_job(visualize.build_render_job(old), tmp_path)
    new_path = visualize.save_job(visualize.build_render_job(new), tmp_path)
    # 显式拉开修改时间：文件总线按 mtime 取最新
    import os
    import time

    stamp = time.time()
    os.utime(old_path, (stamp - 10, stamp - 10))
    os.utime(new_path, (stamp, stamp))

    job = module.sync_from_files(tmp_path)
    assert job is not None
    assert job["snapshot_id"] == new.snapshot_id
    ack_path = tmp_path / "acks" / f"{new.snapshot_id}.json"
    assert ack_path.is_file()
    # ack 要能自证"这一次演了什么"：假引擎里没有 Actor，命中数必须如实写 0 而不是省略
    ack = json.loads(ack_path.read_text(encoding="utf-8"))
    assert ack["engine"] == "unreal"
    assert ack["hit_count"] == 0
    assert ack["acked_at"]
    # 认领后可被 scene_render 读到（delivery 从 pending 变 acked）
    assert visualize.delivery_state(new.snapshot_id, tmp_path) == "acked"


def test_sync_from_files_without_jobs_is_quiet(twin, tmp_path):
    module, state = twin
    assert module.sync_from_files(tmp_path) is None
    assert any("没有渲染作业" in line for line in state["logs"])
