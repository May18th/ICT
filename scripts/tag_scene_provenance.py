"""给 `samples/assets/urban/scene.json` 的每个对象打数据来源标注。

**为什么需要**：项目在 `contracts.ProvenanceLevel` 里定义了四级来源（real / derived /
assumption / synthetic），但 194 个场景对象一个都没标。结果是：CSV 层每行都能追到
URL（见 `work_orders.csv` 与 `response_events_chengdu_20200811.csv` 的 source_url 列），
场景层却查不到——评委点着 3D 里那台泵站问"这是真事吗"，数据答不出来。

**标注口径**（逐项核对过原始文件，不是按 kind 一刀切）：

| 对象 | 级别 | 依据 |
| --- | --- | --- |
| 成都市防汛抗旱应急预案（2020 年） | real | 权威媒体双源全文转载，A/B 逐字一致，原件被 WAF 拦 |
| 成都市防汛抗旱应急预案（2026 年度修订） | real | 成都市应急管理局官网 PDF 原件，已记录 URL 与 sha256 |
| 其余 192 个 | synthetic | 区级场景为演示构建：路名、泵站、工单、部门、区级预案 |

建筑 120 个特意核过：twin_id 是 `B-A-03-000` 这类合成编号，属性只有 district 与
height_m，**没有 osm_id**。真实 OSM 实体（`RD-DONGHUAMEN` 这类 24 个）在
`semantic_entities_legacy_osm.json` 里，按 `scene_binding.py` 的说明已降级为背景底图，
不再充当事件层几何——所以建筑必须标 synthetic，标 real 是错的。

**幂等**：重复运行结果逐字节一致；已在 attributes 里的 provenance 会被覆盖成当前规则
的结果，不会累积。

**注意**：`scene.json` 由 `decision_poc.scene_export` 生成，重跑导出会丢掉本脚本写的
标注——导出后需要再跑一次本脚本（`--check` 会提示未标注的对象）。

用法：
    python scripts/tag_scene_provenance.py --check     # 只报告，不改文件
    python scripts/tag_scene_provenance.py             # 写入标注
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCENE_PATH = ROOT / "samples" / "assets" / "urban" / "scene.json"

LEVEL_LABELS = {
    "real": "真实",
    "derived": "推算",
    "assumption": "假设",
    "synthetic": "演示构建",
}

# 逐个体核对过的真实对象：twin_id -> (级别, 依据)
REAL_TWINS: dict[str, tuple[str, str]] = {
    "TW-plan-成都市防汛抗旱应急预案-2020-年-全文转载留存": (
        "real",
        "权威媒体双源全文转载（四川发布 / 红星新闻），两源对同一段条款逐字一致；"
        "原件地址 HTTP 412 被 WAF 拦截，未取到原件",
    ),
    "TW-plan-成都市防汛抗旱应急预案-2026-年度修订-响应分级条款节选": (
        "real",
        "成都市应急管理局官网 PDF 原件，已记录 URL 与 PDF sha256（见文件头来源说明）",
    ),
}

# 其余对象按 kind 给依据：说明它们为什么是演示构建，而不是只说"假"
KIND_BASIS: dict[str, str] = {
    "plan": "区级预案/作业规程，文件编号与发布单位为演示构建，非真实文件",
    "weather": "演示事件（2026-10-29 时间窗）的预警行；real_20260801 变体另行装载 ERA5 真实雨量",
    "region": "演示片区编号，非行政区划代码",
    "road": "路名与折线为演示布局，坐标是本地米制示意（非测绘精度）",
    "building": "街区体量填充，非测绘建筑；真实 OSM 几何另存为背景底图（legacy_osm）",
    "pump_station": "演示设施台账（泵站编号与装机容量为情景设定）",
    "drainage": "演示设施台账（雨水口 / 泵站，与排水设施 CSV 同源）",
    "water": "地下给水管线段，演示管线台账",
    "gas": "地下燃气管线段，演示管线台账",
    "power": "地下电力管线段，演示管线台账",
    "telecom": "地下通信管线段，演示管线台账",
    "underpass": "演示下穿通道台账",
    "work_site": "演示在建占道施工计划（含开挖深度，用于管线碰撞判断）",
    "incident": "演示工单，与 work_orders.csv 同源",
    "action": "演示处置动作，与应急预案条款逐条对应",
    "resource": "演示应急资源台账（数量为情景设定，非实时库存）",
    "department": "演示区级部门职责表（含 authority 与 escalation_note）",
}

DEFAULT_BASIS = "演示构建的场景对象"


def classify(obj: dict) -> tuple[str, str]:
    """返回 (级别, 依据)。真实对象逐个匹配，其余按 kind 取依据。"""
    twin_id = str(obj.get("twin_id", ""))
    if twin_id in REAL_TWINS:
        return REAL_TWINS[twin_id]
    kind = str(obj.get("kind", ""))
    return "synthetic", KIND_BASIS.get(kind, DEFAULT_BASIS)


def tag(scene: dict) -> tuple[dict, Counter]:
    """在 attributes 里写入 provenance / provenance_basis，返回统计。"""
    counts: Counter = Counter()
    for obj in scene.get("objects") or []:
        level, basis = classify(obj)
        attrs = obj.setdefault("attributes", {})
        attrs["provenance"] = level
        attrs["provenance_basis"] = basis
        counts[level] += 1
    return scene, counts


def unlabelled(scene: dict) -> list[str]:
    return [
        str(o.get("twin_id", "?"))
        for o in (scene.get("objects") or [])
        if "provenance" not in (o.get("attributes") or {})
    ]


def render(counts: Counter, total: int, *, wrote: bool) -> str:
    lines = [f"{'已写入' if wrote else '待写入'} {total} 个对象的来源标注：", ""]
    for level in ("real", "derived", "assumption", "synthetic"):
        n = counts.get(level, 0)
        if not n:
            continue
        lines.append(f"  {LEVEL_LABELS[level]:<6} {level:<10} {n:>4} 个  ({n * 100 / total:.0f}%)")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="给场景对象打数据来源标注（幂等）")
    parser.add_argument("--check", action="store_true", help="只报告未标注对象，不写文件")
    parser.add_argument("--out", default=str(SCENE_PATH), help="写入路径，默认覆盖原 scene.json")
    args = parser.parse_args(argv)

    if not SCENE_PATH.is_file():
        print(f"[失败] 找不到场景文件：{SCENE_PATH}", file=sys.stderr)
        return 1

    scene = json.loads(SCENE_PATH.read_text(encoding="utf-8"))
    objects = scene.get("objects") or []
    if not objects:
        print("[失败] scene.json 里没有 objects", file=sys.stderr)
        return 1

    if args.check:
        missing = unlabelled(scene)
        if missing:
            print(f"[未标注] {len(missing)}/{len(objects)} 个对象缺 provenance，前 5 个：")
            for twin_id in missing[:5]:
                print(f"  - {twin_id}")
            print("\n运行 `python scripts/tag_scene_provenance.py` 补齐。")
            return 2
        tally: Counter = Counter()
        for obj in objects:
            tally[(obj.get("attributes") or {}).get("provenance", "?")] += 1
        print(render(tally, len(objects), wrote=False).replace("待写入", "已标注"))
        return 0

    tagged, counts = tag(scene)
    Path(args.out).write_text(
        # 钉 \n：Windows 默认 CRLF 会让 scene.json 与场景覆盖目录逐字节比对假失败
        json.dumps(tagged, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )
    print(render(counts, len(objects), wrote=True))
    print(f"\n写入 {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
