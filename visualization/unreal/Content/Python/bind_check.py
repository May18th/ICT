"""启动自检：把 v2 语义状态应用到当前场景，并把命中数写进日志。

用法（图形模式）：
    UnrealEditor.exe UrbanTwinDemo.uproject -ExecutePythonScript="<此文件>" -windowed

为什么用 -ExecutePythonScript 而不是 MCP：MCP 只暴露工具编排沙箱，不能 import unreal，
而本脚本要调用 urban_twin_state 的 apply_scenario/highlight。
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import unreal  # noqa: E402  (只在编辑器内可用)

TARGETS = {
    "TW-underpass-UP-XH-101": ("underpass", "backflow_high"),
    "TW-pump_station-DF-PS-01": ("pump_station", "overload"),
}


def _ensure_tags():
    """给已存在的语义代理补齐 kind:/state: 标签（MCP 侧已打过，这里做幂等兜底）。"""
    import urban_twin_state as twin

    added = []
    for actor in twin._all():
        tags = twin._tags(actor)
        twin_id = next((tag.split(":", 1)[1] for tag in tags if tag.startswith("twin:")), None)
        if twin_id not in TARGETS:
            continue
        kind, state = TARGETS[twin_id]
        for tag in (f"kind:{kind}", f"state:{state}"):
            if tag in tags:
                continue
            try:
                actor.tags.append(unreal.Name(tag))
                added.append(f"{twin_id}<-{tag}")
            except Exception as exc:
                unreal.log_warning(f"[bind_check] 无法追加标签 {tag} 到 {twin_id}: {exc}")
    return added


def main() -> None:
    import urban_twin_state as twin

    twin.ensure_level(twin._level_path())
    added = _ensure_tags()
    counts = twin.apply_scenario("flood")
    # BIND_CHECK_HIGHLIGHT=1 时才覆盖高亮材质；默认保留告警材质，便于截图核对颜色。
    if os.environ.get("BIND_CHECK_HIGHLIGHT", "0") == "1":
        hits = twin.highlight([twin_id for twin_id in TARGETS])
        payload = {"tags_added": added, "scenario": counts, "highlight_hits": hits}
    else:
        payload = {"tags_added": added, "scenario": counts, "highlight": "skipped"}
    unreal.log("[bind_check] " + json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
