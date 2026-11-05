# -*- coding: utf-8 -*-
"""P11 渲染执行器：让 `RenderJob` 真的出一段片子，并把回执写回去。

为什么需要这一层
----------------
`visualize.py` 的设计里写明"本模块不写'渲染完成'"——它只落盘作业，认领状态由引擎侧写回。
在接 UE 之前，这个"引擎侧"是空的：`scene_render` 永远返回 `delivery=pending`，
平台 Agent 调完工具拿不到任何可看的东西。本模块补的就是这个执行器。

三种出片模式（`--mode`）
------------------------
| 模式 | 做什么 | 耗时 | 用途 |
| --- | --- | --- | --- |
| `asset` | 复用已渲染好的成片（按 `scenario` 查表），复制到作业目录并算 sha256 | 秒级 | 比赛现场演示、CI |
| `blender` | 起 Blender 无头渲染，跑完再合成 mp4 | 分钟~小时 | 出新的画质/新镜头 |
| `auto` | 有资产走 asset，没有走 blender | — | 默认 |

三条硬约束（与 `visualize.py` 一致，违反即失真）
----------------------------------------------
1. **只吃 `snapshot_id` 指到的作业文件**，不接受位姿/材质/镜头参数；
2. **幂等**：同一 `snapshot_id` 重复调用，若 ack 里的 sha256 与产物一致则直接复用，不重渲；
3. **不谎报**：渲失败就不写 ack，`delivery` 保持 `pending`，错误原样上抛。

用法
----
    python -m decision_poc.render_worker --latest --mode asset
    python -m decision_poc.render_worker --snapshot-id 3f2a... --mode asset --json
    python -m decision_poc.render_worker --latest --mode blender --force

Blender 模式需要的环境变量（缺任一则明确报错，不静默降级）：

    DECISION_POC_BLENDER_EXE       blender 可执行文件
    DECISION_POC_BLENDER_SCENE     .blend 路径
    DECISION_POC_BLENDER_SCRIPT    render_p5_incident.py 路径
    DECISION_POC_BLENDER_STITCH    可选，合成脚本（默认取同目录 build_video_p9.py）
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Sequence

from . import visualize

REPO = Path(__file__).resolve().parents[2]

#: `event_id` → 已渲染成片（相对仓库根）。命中即秒级出片。
#: 为什么按工单而不是按 scenario 索引：一段片子拍的是**某个具体工单的现场**，
#: 同一个 scenario 下不同工单的画面并不通用，按 scenario 复用等于拿别的现场糊弄。
PRERENDERED_EVENTS: dict[str, str] = {
    "WO-20261029-007": "docs/competition/video-clips/VD02-B-incident-P9.mp4",
}

#: `scenario` → 成片（只在该场景确实只有一套片子时使用，避免张冠李戴）
PRERENDERED_SCENARIOS: dict[str, str] = {}

#: 覆盖资产目录（不想改代码时用）：把 `<scenario>.mp4` 丢进去即可
ASSET_DIR_ENV = "DECISION_POC_RENDER_ASSETS"


class RenderUnavailable(RuntimeError):
    """渲染环境不满足（缺 Blender、缺场景、缺资产）。"""


class RenderFailed(RuntimeError):
    """渲染跑起来了但没产出可用成片。"""


@dataclass
class RenderOutcome:
    snapshot_id: str
    mode: str
    status: str                      # acked | reused | failed
    artifact: dict[str, Any] = field(default_factory=dict)
    detail: str = ""
    elapsed_s: float = 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "snapshot_id": self.snapshot_id,
            "mode": self.mode,
            "status": self.status,
            "artifact": self.artifact,
            "detail": self.detail,
            "elapsed_s": round(self.elapsed_s, 2),
        }


# --- 基础工具 -------------------------------------------------------------


def sha256_of(path: Path, chunk: int = 1 << 20) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(chunk), b""):
            digest.update(block)
    return digest.hexdigest()


def clip_path(snapshot_id: str, root: Path | str | None = None) -> Path:
    return visualize.visual_root(root) / "clips" / f"{snapshot_id}.mp4"


def _asset_for(event_id: str, scenario: str) -> Path | None:
    env_dir = os.environ.get(ASSET_DIR_ENV)
    if env_dir:
        for key in (event_id, scenario):
            if not key:
                continue
            candidate = Path(env_dir) / f"{key}.mp4"
            if candidate.is_file():
                return candidate
    for key in (event_id, scenario):
        if not key:
            continue
        rel = PRERENDERED_EVENTS.get(key) or PRERENDERED_SCENARIOS.get(key)
        if rel:
            path = (REPO / rel).resolve()
            if path.is_file():
                return path
    return None


def _load_job(snapshot_id: str, root: Path | str | None) -> visualize.RenderJob:
    try:
        return visualize.load_job(snapshot_id, root)
    except KeyError as exc:
        raise RenderUnavailable(f"没有该快照的渲染作业：{snapshot_id}（先调 scene_render）") from exc


def already_rendered(
    snapshot_id: str, root: Path | str | None = None
) -> dict[str, Any] | None:
    """已 ack 且产物仍在、哈希对得上 → 返回 ack；否则 None。"""
    ack = visualize.read_ack(snapshot_id, root)
    if not ack:
        return None
    artifact = ack.get("artifact") or {}
    rel = artifact.get("path")
    if not rel:
        return None
    target = Path(rel)
    if not target.is_absolute():
        target = visualize.visual_root(root) / rel
    if not target.is_file():
        return None
    if artifact.get("sha256") and sha256_of(target) != artifact["sha256"]:
        return None
    return ack


# --- 出片 -----------------------------------------------------------------


def _finish(
    snapshot_id: str,
    source: Path,
    *,
    mode: str,
    engine: str,
    root: Path | str | None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """把成片收进作业目录、算哈希、写 ack。返回 artifact 描述。"""
    dest = clip_path(snapshot_id, root)
    dest.parent.mkdir(parents=True, exist_ok=True)
    if source.resolve() != dest.resolve():
        shutil.copy2(source, dest)
    artifact = {
        "kind": "video/mp4",
        "path": str(dest.relative_to(visualize.visual_root(root))),
        "sha256": sha256_of(dest),
        "bytes": dest.stat().st_size,
        "render_mode": mode,
        "rendered_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    visualize.write_ack(
        snapshot_id, engine=engine, root=root, artifact=artifact, extra=extra
    )
    return artifact


def _render_asset(job: visualize.RenderJob, root: Path | str | None) -> dict[str, Any]:
    scene = job.scene or {}
    event_id = str(scene.get("event_id") or "")
    scenario = str(scene.get("scenario") or "")
    source = _asset_for(event_id, scenario)
    if source is None:
        raise RenderUnavailable(
            f"event_id={event_id or '未知'} / scenario={scenario or '未知'} 没有预渲染资产；"
            f"用 --mode blender 出片，或把 {event_id or scenario}.mp4 放进 ${ASSET_DIR_ENV}"
        )
    return _finish(
        job.snapshot_id, source, mode="asset", engine="blender-asset", root=root,
        extra={"source_asset": str(source.relative_to(REPO)) if source.is_relative_to(REPO) else str(source)},
    )


def _blender_env() -> tuple[Path, Path, Path, Path | None]:
    keys = {
        "exe": "DECISION_POC_BLENDER_EXE",
        "scene": "DECISION_POC_BLENDER_SCENE",
        "script": "DECISION_POC_BLENDER_SCRIPT",
    }
    missing = [name for name in keys.values() if not os.environ.get(name)]
    if missing:
        raise RenderUnavailable(
            "Blender 模式缺少环境变量：" + ", ".join(missing)
            + "（asset 模式不需要；不要静默降级成假渲染）"
        )
    exe = Path(os.environ[keys["exe"]])
    scene = Path(os.environ[keys["scene"]])
    script = Path(os.environ[keys["script"]])
    for path, label in ((exe, "BLENDER_EXE"), (scene, "SCENE"), (script, "SCRIPT")):
        if not path.exists():
            raise RenderUnavailable(f"{label} 不存在：{path}")
    stitch_env = os.environ.get("DECISION_POC_BLENDER_STITCH")
    stitch = Path(stitch_env) if stitch_env else script.with_name("build_video_p9.py")
    return exe, scene, script, (stitch if stitch.is_file() else None)


def _render_blender(
    job: visualize.RenderJob, root: Path | str | None, *, samples: int, frames: int
) -> dict[str, Any]:
    exe, scene, script, stitch = _blender_env()
    visual_root = visualize.visual_root(root)
    frames_dir = visual_root / "frames" / job.snapshot_id
    frames_dir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["B_INC_FRAMES"] = str(frames_dir)
    for shot in ("S1", "S2", "S3", "S5"):
        cmd = [str(exe), "-b", str(scene), "--python", str(script),
               "--", "full", shot, str(frames), str(samples)]
        proc = subprocess.run(cmd, capture_output=True, text=True, env=env, errors="replace")
        if proc.returncode != 0:
            raise RenderFailed(
                f"Blender 退出码 {proc.returncode}（{shot}）：{(proc.stderr or '')[-600:]}"
            )
    if stitch is None:
        raise RenderUnavailable(
            "缺少合成脚本；设置 DECISION_POC_BLENDER_STITCH 指向 build_video_p9.py"
        )
    out_video = visual_root / f"{job.snapshot_id}.mp4"
    env2 = dict(os.environ)
    env2["DECISION_POC_STITCH_OUT"] = str(out_video)
    proc = subprocess.run([sys.executable, str(stitch)], capture_output=True, text=True,
                          env=env2, errors="replace")
    if proc.returncode != 0 or not out_video.is_file():
        raise RenderFailed(f"合成失败：{(proc.stderr or proc.stdout or '')[-600:]}")
    return _finish(
        job.snapshot_id, out_video, mode="blender", engine="blender",
        root=root, extra={"samples": samples, "frames_per_shot": frames},
    )


def render(
    snapshot_id: str,
    *,
    mode: str = "auto",
    root: Path | str | None = None,
    force: bool = False,
    samples: int = 12,
    frames: int = 60,
) -> RenderOutcome:
    """认领一份作业并出片。幂等：已 ack 且哈希对得上直接 reused。"""
    if mode not in ("auto", "asset", "blender"):
        raise ValueError(f"未知模式 {mode}，可选 auto/asset/blender")
    started = time.time()
    job = _load_job(snapshot_id, root)

    if not force:
        existing = already_rendered(snapshot_id, root)
        if existing is not None:
            return RenderOutcome(
                snapshot_id=snapshot_id,
                mode=mode,
                status="reused",
                artifact=existing.get("artifact", {}),
                detail="ack 已存在且产物哈希一致，跳过重渲",
                elapsed_s=time.time() - started,
            )

    try:
        if mode in ("auto", "asset"):
            try:
                artifact = _render_asset(job, root)
            except RenderUnavailable:
                if mode == "asset":
                    raise
                artifact = _render_blender(job, root, samples=samples, frames=frames)
        else:
            artifact = _render_blender(job, root, samples=samples, frames=frames)
    except (RenderUnavailable, RenderFailed) as exc:
        return RenderOutcome(
            snapshot_id=snapshot_id, mode=mode, status="failed",
            detail=str(exc), elapsed_s=time.time() - started,
        )

    return RenderOutcome(
        snapshot_id=snapshot_id, mode=mode, status="acked",
        artifact=artifact, detail="已出片并写回 ack",
        elapsed_s=time.time() - started,
    )


def render_latest(**kwargs: Any) -> RenderOutcome:
    snapshot_id = visualize.latest_snapshot_id(kwargs.get("root"))
    if not snapshot_id:
        raise RenderUnavailable("作业目录里没有任何 snapshot，先调 scene_render")
    return render(snapshot_id, **kwargs)


# --- CLI ------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="decision_poc.render_worker")
    parser.add_argument("--snapshot-id")
    parser.add_argument("--latest", action="store_true", help="渲染最近一次作业")
    parser.add_argument("--mode", default="auto", choices=["auto", "asset", "blender"])
    parser.add_argument("--force", action="store_true", help="忽略已有 ack，强制重渲")
    parser.add_argument("--samples", type=int, default=12)
    parser.add_argument("--frames", type=int, default=60)
    parser.add_argument("--root", help="可视化作业根目录（默认 artifacts/visual）")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(list(argv) if argv is not None else None)

    if not args.snapshot_id and not args.latest:
        parser.error("需要 --snapshot-id 或 --latest")
    try:
        outcome = (
            render(args.snapshot_id, mode=args.mode, root=args.root, force=args.force,
                   samples=args.samples, frames=args.frames)
            if args.snapshot_id
            else render_latest(mode=args.mode, root=args.root, force=args.force,
                               samples=args.samples, frames=args.frames)
        )
    except RenderUnavailable as exc:
        print(f"RENDER_UNAVAILABLE {exc}", file=sys.stderr)
        return 2

    payload = outcome.as_dict()
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"{payload['status']} {payload['snapshot_id']} mode={payload['mode']} "
              f"{payload['elapsed_s']}s {payload['detail']}")
    return 0 if outcome.status in ("acked", "reused") else 1


if __name__ == "__main__":
    raise SystemExit(main())
