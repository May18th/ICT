"""Unreal 端状态切换与回放：把"结论"变成"看得见的变化"。

关卡由 `build_urban_level.py` 按布局搭好，本脚本只做三件事，且全部只看 Actor 的 tags
（`twin:<id>` / `kind:<kind>` / `state:<state>`），不认对象名字：

1. `apply_scenario`：切场景状态——水面显隐、封路变红、施工段变黄；
2. `highlight`：把某一跳涉及的对象换成高亮材质，其余恢复；
3. `show_step` / `replay`：按 `samples/replay/core_run.json` 逐步推进，面板上写该跳的
   结论与证据数量。回放的每一步都来自一次真实推演的结果，脚本不生成任何业务判断。

现场用法（编辑器 Python 控制台）：

    import urban_twin_state as s
    s.sync_from_api()                # 取最新渲染作业并演出（智能体调的）
    s.sync_from_api("snap-xxxx")     # 指定快照
    s.watch()                        # 挂机监听：智能体每调一次 scene_render 就换一次场
    s.apply_scenario("closure")      # 切到"封路"状态（手动，仅排障用）
    s.show_step(3)                   # 播到第 4 跳（冲突）
    s.replay_step(1)                 # 前进 / 后退

两条取数通路，按环境选：

- `sync_from_api()` / `watch()`：走 `web_demo` 的 HTTP 端点（引擎与 Python 分开跑时用）；
- `sync_from_files()` / `watch_files()`：直接读 `artifacts/visual/jobs/` 下最新作业并写回
  `acks/`。**同机演示优先用这条**——我们实测过端口被占/被 Windows 排除的情况
  （WinError 10013），文件总线没有这个失败模式。
- `selftest_render_job()`：不依赖界面，校验最新作业的字段契约并打印 PASS/FAIL。

无界面验证：

    UnrealEditor-Cmd.exe UrbanTwinDemo.uproject -run=pythonscript \\
        -script=".../urban_twin_state.py" -unattended -nosplash -nullrhi \\
        -UrbanFiles=1 -UrbanLevel=/Game/UrbanTwin/Maps/UrbanTwinDemo

`-UrbanLevel` 不能省：commandlet 形态下命令行给的地图不会被载入，脚本会落在空关卡上
（表现为 ack 的 `level` 是 `/Temp/Untitled_0`、`twin_tag_count` 为 0）。
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import unreal

ROOT = Path(__file__).resolve().parents[4]
REPLAY_PATH = ROOT / "samples" / "replay" / "core_run.json"
MATERIAL_ROOT = "/Game/UrbanTwin/Materials"

PANEL_LABEL = "TXT_ReplayPanel"
_current_step = -1
# 最近一次 highlight() 命中的 Actor 数：写进 ack，避免"抢到 0 个对象也叫已演出"
_last_hits = 0
# 最近一次 highlight() 想要但关卡里没有的 twin_id。空缺必须显式写出来，
# 不能因为"没报错"就当成高亮成功——这正是 S19 验收一直收不了口的原因。
_last_misses: list[str] = []


def log(message: str) -> None:
    unreal.log(f"[UrbanTwin] {message}")


# --- 基础设施 -----------------------------------------------------------


def _actors():
    return unreal.get_editor_subsystem(unreal.EditorActorSubsystem)


def _all():
    return _actors().get_all_level_actors()


def _tags(actor) -> list[str]:
    return [str(tag) for tag in actor.get_editor_property("tags")]


def _has_tag(actor, prefix: str) -> bool:
    return any(tag.startswith(prefix) for tag in _tags(actor))


def _by_twin(twin_id: str):
    wanted = f"twin:{twin_id}"
    return [actor for actor in _all() if wanted in _tags(actor)]


def _material(name: str):
    path = f"{MATERIAL_ROOT}/{name}"
    asset = unreal.EditorAssetLibrary.load_asset(path)
    if asset is None:
        log(f"材质缺失 {path}，先运行 build_urban_level.py")
    return asset


def _swap_material(actor, material) -> bool:
    """换材质槽 0。返回**是否真的换上了**——调用方不要假设一定成功。

    2026-11-02 修：原来返回 None，`highlight()` 无论成败都 `hits += 1`，
    于是 ack 报"命中 1 个"，画面却三张字节完全相同。命中数必须只统计换成功的。
    """
    component = actor.get_component_by_class(unreal.StaticMeshComponent)
    if component is None or material is None:
        return False
    component.set_material(0, material)
    return True


# --- 场景状态 -----------------------------------------------------------


def apply_scenario(scenario: str) -> dict[str, int]:
    """normal / flood / closure / construction：只改显示，不动语义。"""
    show_water = scenario in {"flood", "closure", "construction"}
    show_closure = scenario in {"closure", "construction"}
    counts = {"water": 0, "closed": 0, "construction": 0, "pump_alerts": 0, "underpass_alerts": 0}
    water_material = _material("M_FloodWater")

    for actor in _all():
        tags = _tags(actor)
        if "kind:water" in tags:
            actor.set_actor_hidden_in_game(not show_water)
            try:
                actor.set_is_temporarily_hidden_in_editor(not show_water)
            except Exception:  # 编辑器隐藏是辅助手段，失败不影响游戏内可见性
                pass
            if show_water:
                _swap_material(actor, water_material)
                counts["water"] += 1
            continue
        if "kind:pump_station" in tags:
            # 泵站负荷：推演 pump_load 的 overload/high_load 用告警材质表达
            pump_state = next((tag.split(":", 1)[1] for tag in tags if tag.startswith("state:")), "normal")
            if pump_state in {"overload", "high_load"}:
                _swap_material(actor, _material("M_PumpStation_Alert") or _material("M_PumpStation"))
                counts["pump_alerts"] += 1
            continue
        if "kind:underpass" in tags:
            # 下穿通道：倒灌状态用告警材质表达
            up_state = next((tag.split(":", 1)[1] for tag in tags if tag.startswith("state:")), "normal")
            if up_state in {"backflow_high", "backflow_medium"}:
                _swap_material(actor, _material("M_Underpass_Alert") or _material("M_Underpass"))
                counts["underpass_alerts"] += 1
            continue
        if "kind:road" not in tags:
            continue
        state = next((tag.split(":", 1)[1] for tag in tags if tag.startswith("state:")), "normal")
        if state == "closed":
            material = _material("M_Road_Closed") if show_closure else _material("M_Road_Normal")
            _swap_material(actor, material)
            counts["closed"] += 1 if show_closure else 0
        elif state == "under_construction":
            _swap_material(actor, _material("M_Road_Construction"))
            counts["construction"] += 1
    log(f"场景 {scenario}: 水面 {counts['water']} / 封路 {counts['closed']} / 施工 {counts['construction']}")
    return counts


def highlight(twin_ids: list[str]) -> int:
    """把给定对象换高亮材质，其余恢复本色。返回命中的 Actor 数。"""
    global _last_hits, _last_misses
    wanted = set(twin_ids)
    highlight_material = _material("M_Highlight")
    normal_roads = _material("M_Road_Normal")
    closed_roads = _material("M_Road_Closed")
    construction_roads = _material("M_Road_Construction")
    base = {
        "normal": normal_roads,
        "closed": closed_roads,
        "under_construction": construction_roads,
    }
    hits = 0
    matched: set[str] = set()
    for actor in _all():
        tags = _tags(actor)
        twin = next((tag.split(":", 1)[1] for tag in tags if tag.startswith("twin:")), None)
        if twin is None:
            continue
        if twin in wanted:
            if _swap_material(actor, highlight_material):
                matched.add(twin)
                hits += 1
            else:
                log(f"高亮失败 {twin}：Actor {actor.get_actor_label()} 没有可换的 StaticMeshComponent")
            continue
        state = next((tag.split(":", 1)[1] for tag in tags if tag.startswith("state:")), "")
        kind = next((tag.split(":", 1)[1] for tag in tags if tag.startswith("kind:")), "")
        if kind == "road":
            _swap_material(actor, base.get(state, normal_roads))
        elif kind in {"pump_station", "intake"}:
            if kind == "pump_station" and state in {"overload", "high_load"}:
                _swap_material(actor, _material("M_PumpStation_Alert") or _material("M_PumpStation"))
            else:
                _swap_material(actor, _material("M_PumpStation" if kind == "pump_station" else "M_Intake"))
        elif kind == "underpass":
            alert = _material("M_Underpass_Alert") if state in {"backflow_high", "backflow_medium"} else None
            _swap_material(actor, alert or _material("M_Underpass") or highlight_material)
    _last_hits = hits
    _last_misses = sorted(wanted - matched)
    if _last_misses:
        log(f"高亮空缺 {len(_last_misses)} 个（关卡内无 twin: 标签）: {_last_misses}")
    return hits


def preflight(job: dict) -> dict:
    """逐跳核对：这份作业想要的每个 twin_id，关卡里到底有没有 Actor。

    只读，不改场景。写进 ack 的那一份"命中/空缺"就是这一趟演出的可核对凭证——
    `scene_render` 的 delivery 读到 acked 之后，还要能看出"演了几跳、哪一跳缺对象"。
    """
    available: dict[str, list[str]] = {}
    for actor in _all():
        for tag in _tags(actor):
            if tag.startswith("twin:"):
                available.setdefault(tag.split(":", 1)[1], []).append(actor.get_actor_label())
    steps = (job.get("scene") or {}).get("steps") or []
    rows = []
    for step in steps:
        wanted = list(step.get("highlights") or [])
        missing = [twin for twin in wanted if twin not in available]
        rows.append(
            {
                "index": step.get("index"),
                "stage": step.get("stage"),
                "planned": len(wanted),
                "hit": len(wanted) - len(missing),
                "missing": missing,
            }
        )
    total_planned = sum(row["planned"] for row in rows)
    total_hit = sum(row["hit"] for row in rows)
    log(
        f"[preflight] 关卡 twin 标签 {len(available)} 个 / 作业 {len(rows)} 跳 / "
        f"计划高亮 {total_planned} 次、可命中 {total_hit} 次"
    )
    for row in rows:
        if row["missing"]:
            log(f"[preflight]   第 {row['index']} 跳 {row['stage']} 缺 {row['missing']}")
    return {
        "distinct_twin_tags": len(available),
        "tagged_actors": sum(len(labels) for labels in available.values()),
        "steps": rows,
        "total_planned": total_planned,
        "total_hit": total_hit,
    }


# --- 回放 ---------------------------------------------------------------


def _panel():
    for actor in _all():
        if actor.get_actor_label() == PANEL_LABEL:
            return actor
    return None


def _set_panel(text: str) -> None:
    actor = _panel()
    if actor is None:
        location = unreal.Vector(0, 0, 3000)
        actor = _actors().spawn_actor_from_class(
            unreal.TextRenderActor, location, unreal.Rotator(0, 0, 0)
        )
        actor.set_actor_label(PANEL_LABEL)
    component = actor.get_component_by_class(unreal.TextRenderComponent)
    if component is not None:
        component.set_text(unreal.Text(text))
        component.set_world_size(600.0)


def load_replay(path: Path = REPLAY_PATH) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def show_step(index: int, replay: dict | None = None) -> int:
    """播到第 index 跳：高亮该跳对象 + 面板写结论。越界不报错，停在最后一跳。"""
    global _current_step
    replay = replay or load_replay()
    steps = replay.get("steps", [])
    if not steps:
        log("回放脚本为空")
        return -1
    index = max(0, min(index, len(steps) - 1))
    step = steps[index]
    hits = highlight(list(step.get("highlights", [])))
    title = f"[{index + 1}/{len(steps)}] {step.get('title', step['stage'])}"
    _set_panel(f"{title}\n{step.get('narrative', '')}\n证据 {len(step.get('evidence', []))} 条")
    _current_step = index
    log(f"第 {index + 1} 跳 {step['stage']}({step['status']}) 命中 {hits} 个对象")
    return index


def replay_step(delta: int = 1) -> int:
    return show_step(_current_step + delta)


def replay_actions(replay: dict | None = None) -> list[str]:
    """处置顺序：一次性返回给面板/日志，动画由关卡里的序列或现场口述衔接。"""
    replay = replay or load_replay()
    return [f"{item['order']}. {item['action']}" for item in replay.get("actions", [])]


# --- 从 MCP 取作业（P10） -------------------------------------------------
#
# 分工：智能体调 `scene_render` 落盘一份 RenderJob，这里把作业取回来演。
# 取数走 web_demo 的 `/api/scene/...`（起在 8787，不需要 UE 像素流）。
# 演完 POST `/ack` 写回认领状态——`scene_render` 返回的 delivery 读的就是它。
# 因此现场**不需要**在编辑器里手敲 show_step：智能体一调，场景就变。

API_BASE = "http://127.0.0.1:8787"


def _http_json(url: str, *, timeout: float = 2.0, payload: dict | None = None):
    import json as _json
    import urllib.request

    data = None
    headers = {"Accept": "application/json"}
    if payload is not None:
        data = _json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return _json.loads(response.read().decode("utf-8"))


def fetch_job(snapshot_id: str | None = None, base_url: str = API_BASE, timeout: float = 2.0) -> dict:
    url = (
        f"{base_url}/api/scene/{snapshot_id}"
        if snapshot_id
        else f"{base_url}/api/scene/latest"
    )
    return _http_json(url, timeout=timeout)


def apply_job(job: dict) -> dict:
    """把一份 RenderJob 演出来：场景状态 + 第一跳高亮 + 面板（含分配方案）。"""
    scene = job.get("scene") or {}
    panel = job.get("panel") or {}
    apply_scenario(scene.get("scenario", "flood"))
    steps = scene.get("steps") or []
    if steps:
        show_step(0, {"steps": steps, "actions": scene.get("actions") or []})
    else:
        highlight([])
        _set_panel(panel.get("text", ""))
    log(
        f"作业 {job.get('snapshot_id')} / 场景 {scene.get('scenario')} / "
        f"策略 {scene.get('policy_version')} / 面板 {len(panel.get('allocation_lines') or [])} 行"
    )
    return job


def ack_job(snapshot_id: str, base_url: str = API_BASE, timeout: float = 2.0) -> bool:
    try:
        _http_json(f"{base_url}/api/scene/{snapshot_id}/ack", timeout=timeout, payload={})
    except Exception as exc:  # 认领失败不影响演出，只是 delivery 停在 pending
        log(f"ack 失败: {exc}")
        return False
    return True


def sync_from_api(
    snapshot_id: str | None = None, base_url: str = API_BASE
) -> dict | None:
    """取一次作业并演出。服务没起只记日志、不抛异常——演示掉一个环节不至于全崩。"""
    try:
        job = fetch_job(snapshot_id, base_url)
    except Exception as exc:
        log(f"取作业失败（web_demo 服务起了吗？{base_url}）: {exc}")
        return None
    apply_job(job)
    ack_job(str(job.get("snapshot_id")), base_url)
    return job


def watch(
    interval: float = 1.0, max_seconds: float = 0.0, base_url: str = API_BASE
) -> None:
    """轮询最新作业，snapshot_id 变了就重演。

    这是"智能体决定调整策略 → 自动调用可视化工具 → 场景中呈现变化"的落地形态：
    UE 侧不主动判断，只跟最新作业。
    """
    import time

    seen: str | None = None
    started = time.time()
    log(f"监听 {base_url}/api/scene/latest，间隔 {interval}s")
    while True:
        job: dict | None = None
        try:
            job = _http_json(f"{base_url}/api/scene/latest", timeout=2.0)
        except Exception:
            job = None
        if job is not None and job.get("snapshot_id") != seen:
            seen = str(job.get("snapshot_id"))
            apply_job(job)
            ack_job(seen, base_url)
            log(f"已切换作业 {seen}")
        if max_seconds and time.time() - started >= max_seconds:
            return
        time.sleep(max(0.2, interval))


# --- 文件总线（同机优先，不赌端口） ---------------------------------------
#
# 实测教训：这台机器上 8788 被 Windows 拒绝绑定（WinError 10013），8899 已被别的服务占用。
# 同机演示没必要为"两个进程传一个文件"去赌端口，所以把磁盘当总线：
#   Python 侧 scene_render 写 jobs/<snapshot_id>.json
#   UE 侧读最新作业、写 acks/<snapshot_id>.json（scene_render 的 delivery 读它）

VISUAL_ROOT = Path(
    os.environ.get("URBAN_TWIN_VISUAL_ROOT") or (ROOT / "artifacts" / "visual")
)


def _jobs_dir(root=None) -> Path:
    return Path(root or VISUAL_ROOT) / "jobs"


def _acks_dir(root=None) -> Path:
    return Path(root or VISUAL_ROOT) / "acks"


def latest_local_job_path(root=None) -> Path | None:
    """按修改时间取最新作业。演示现场不需要记 snapshot_id。"""
    directory = _jobs_dir(root)
    if not directory.is_dir():
        return None
    jobs = sorted(directory.glob("*.json"), key=lambda path: path.stat().st_mtime)
    return jobs[-1] if jobs else None


def _level_path() -> str:
    """当前关卡路径；只为 ack 留证据，取不到就不写，不猜。"""
    for getter in (
        lambda: unreal.EditorLevelLibrary.get_editor_world().get_path_name(),
        lambda: unreal.get_editor_subsystem(unreal.UnrealEditorSubsystem).get_editor_world().get_path_name(),
    ):
        try:
            return str(getter())
        except Exception:
            continue
    return ""


def ensure_level(path: str | None) -> str:
    """把指定关卡载进来，返回实际关卡路径。

    实测（2026-11-01）：`-run=pythonscript` 是 commandlet 形态，命令行上给的地图**不会被载入**——
    ack 落成了 `/Temp/Untitled_0`，0 个 twin 标签，却照样写"已演出"。所以关卡由脚本自己载，
    载没载上写进日志，最终由 ack 里的 `level` 字段对账。
    """
    if not path:
        return _level_path()
    current = _level_path()
    if current and Path(current).stem == Path(path).stem:
        log(f"关卡已在编辑器里：{current}")
        return current
    if not unreal.EditorAssetLibrary.does_asset_exist(path):
        log(f"关卡不存在：{path}（ack 会按空关卡记录，不当成功）")
        return current
    unreal.get_editor_subsystem(unreal.LevelEditorSubsystem).load_level(path)
    loaded = _level_path()
    log(f"已载入关卡 {path} → {loaded}")
    return loaded


def ack_local(snapshot_id: str, root=None, *, hits: int | None = None, extra: dict | None = None) -> Path:
    """写回认领。带上关卡、命中数与逐跳核对结果：`scene_render` 的 acked 才有可核对的内容。"""
    import datetime

    directory = _acks_dir(root)
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / f"{snapshot_id}.json"
    if target.is_file():  # 旧回执先留档，覆盖证据等于抹掉上一次演出的结果
        stamp = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
        target.replace(target.with_suffix(f".bak-{stamp}.json"))
    payload = {
        "schema_version": "urban-twin-render-ack-v1",
        "snapshot_id": snapshot_id,
        "engine": "unreal",
        "acked_at": datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds"),
    }
    level = _level_path()
    if level:
        payload["level"] = level
    if hits is not None:
        payload["hit_count"] = int(hits)
    if extra:
        payload.update(extra)
    target.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
    return target


def apply_job_file(path, root=None) -> dict:
    job = json.loads(Path(path).read_text(encoding="utf-8"))
    audit = preflight(job)
    apply_job(job)
    ack_local(
        str(job.get("snapshot_id")),
        root,
        hits=_last_hits,
        extra={
            "twin_tag_count": audit["distinct_twin_tags"],
            "planned_highlights": audit["total_planned"],
            "resolved_highlights": audit["total_hit"],
            "steps": audit["steps"],
            "missing_twin_ids": sorted({t for row in audit["steps"] for t in row["missing"]}),
        },
    )
    return job


def sync_from_files(root=None) -> dict | None:
    path = latest_local_job_path(root)
    if path is None:
        log(f"没有渲染作业：{_jobs_dir(root)}（先在 MCP 侧调 scene_render）")
        return None
    return apply_job_file(path, root)


def watch_files(interval: float = 1.0, max_seconds: float = 0.0, root=None) -> None:
    import time

    seen: str | None = None
    started = time.time()
    log(f"监听作业目录 {_jobs_dir(root)}")
    while True:
        path = latest_local_job_path(root)
        if path is not None and path.stem != seen:
            seen = path.stem
            apply_job_file(path, root)
            log(f"已切换作业 {seen}")
        if max_seconds and time.time() - started >= max_seconds:
            return
        time.sleep(max(0.2, interval))


# --- 自检（不依赖界面） ---------------------------------------------------

REQUIRED_STEP_KEYS = ("index", "stage", "title", "highlights")


def selftest_render_job(path=None, root=None) -> dict:
    """校验最新作业是不是 apply_job 能吃的形状。不猜、不修，只报问题。"""
    target = Path(path) if path else latest_local_job_path(root)
    if target is None or not Path(target).is_file():
        log("[selftest] FAIL 没有作业文件")
        return {"ok": False, "job": str(target), "problems": ["没有作业文件"]}

    job = json.loads(Path(target).read_text(encoding="utf-8"))
    scene = job.get("scene") or {}
    panel = job.get("panel") or {}
    problems: list[str] = []
    if job.get("schema_version") != "urban-twin-render-job-v1":
        problems.append(f"schema_version 不是 render-job-v1：{job.get('schema_version')}")
    if scene.get("scenario") not in {"normal", "flood", "closure", "construction"}:
        problems.append(f"scene.scenario 非法：{scene.get('scenario')}")
    steps = scene.get("steps") or []
    if not steps:
        problems.append("scene.steps 为空：UE 没有可演的东西")
    for index, step in enumerate(steps):
        missing = [key for key in REQUIRED_STEP_KEYS if key not in step]
        if missing:
            problems.append(f"steps[{index}] 缺字段 {missing}")
    if not isinstance(panel.get("text"), str) or not panel.get("text"):
        problems.append("panel.text 为空：面板没东西可写")

    report = {
        "ok": not problems,
        "job": str(target),
        "snapshot_id": job.get("snapshot_id"),
        "scenario": scene.get("scenario"),
        "steps": len(steps),
        "problems": problems,
    }
    log(
        f"[selftest] {'PASS' if report['ok'] else 'FAIL'} "
        f"{report['snapshot_id']} / {report['scenario']} / {report['steps']} 跳"
    )
    for problem in problems:
        log(f"[selftest]   ! {problem}")
    return report


# --- 命令行（无界面验证） ------------------------------------------------


def _arg(name: str, default: str | None = None) -> str | None:
    for token in unreal.SystemLibrary.get_command_line().split():
        if token.startswith(f"-{name}="):
            return token.split("=", 1)[1]
    return default


def main() -> None:
    api = _arg("UrbanApi", API_BASE)
    watch_flag = _arg("UrbanWatch")
    files_flag = _arg("UrbanFiles")
    selftest_flag = _arg("UrbanSelftest")
    snapshot = _arg("UrbanSnapshot")
    scenario = _arg("UrbanScenario")
    step = _arg("UrbanStep")
    level_flag = _arg("UrbanLevel")

    def _off(value: str | None) -> bool:
        return bool(value) and value.lower() in {"0", "false", "no"}

    if level_flag:
        ensure_level(level_flag)

    if selftest_flag and not _off(selftest_flag):
        selftest_render_job()
        return
    if files_flag and not _off(files_flag):
        sync_from_files()
        return
    if watch_flag and not _off(watch_flag):
        # 默认走文件总线：同机演示里它没有端口失败模式
        watch_files()
        return
    if snapshot is not None:
        sync_from_api(None if snapshot.lower() == "latest" else snapshot, base_url=api)
        return
    if scenario:
        apply_scenario(scenario)
    if step is not None:
        replay = load_replay()
        show_step(int(step), replay)
        for line in replay_actions(replay)[:3]:
            log("处置 " + line)
    if not scenario and step is None:
        log("未给出 -UrbanFiles/-UrbanSelftest/-UrbanSnapshot/-UrbanScenario/-UrbanStep：仅做导入自检")
        log(f"回放脚本 {REPLAY_PATH.name} 存在: {REPLAY_PATH.exists()}")
        log(f"作业目录 {_jobs_dir()} 最新作业: {latest_local_job_path()}")


main()
