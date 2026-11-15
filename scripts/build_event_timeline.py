"""从台账反推事件时标，替换 build_map2d.py 里手写的 7 跳开关。

为什么需要这一步
----------------
`build_map2d.py` 的 `JUMP_VISUAL` 原本是一组手写布尔值（clog / dispatch /
block / alarm / crew）。它们演起来好看，但回答不了评委那句「这件事到底
几点发生的、有没有台账依据」。

本脚本把台账里**真实存在**的时刻抽出来，逐跳映射：

| 时刻 | 事实 | 台账出处 |
|---|---|---|
| 07:30 | 橙色预警发布 | weather_alerts.csv `issued_at` |
| 08:00 | 预警生效、降雨开始（= 事件零点） | weather_alerts.csv `valid_from` |
| 08:30 | 半幅封闭生效（施工占道，道路中心 + 交警联合） | notice_road_closure.md `生效时间` |
| 09:20 | 兴华路 K0+320 积水 30cm 上报 → 研判扩大全幅 | work_orders.csv WO-20261029-007 |
| 09:30 | 滨河路 K1+050 积水 18cm 上报 | work_orders.csv WO-20261029-011 |
| 09:35 | 燃气基坑沉陷上报 | work_orders.csv WO-20261029-009 |
| 10-26 15:40 | 1# 泵组电机故障报修（**事件前**，仍未闭环） | work_orders.csv WO-20261026-013 |

口径纪律
--------
每个开关都带 `basis` 字段，只有两种值：

- `ledger`  —— 台账里有明确时刻或状态，可直接指出出处
- `derived` —— 台账没有时刻，按其它依据推演（例如「抢险队到场」台账里
               只有工单创建时间，没有到场时间）

页面据 `basis` 决定标注颜色：全 ledger 时不加额外提示，出现 derived 时
图注写明「部分时刻为推演」。这条降级路径与水位曲线一致——不能让人以为
推演出来的时刻也是台账里抄的。

输出 `out/map2d/event_timeline.json`，由 `build_map2d.py` 读入。
"""

from __future__ import annotations

import csv
import json
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCENE_DIR = ROOT / "samples" / "assets" / "urban"
OUT = ROOT / "out" / "map2d" / "event_timeline.json"

# 与 build_hydro_curve.py 的采样点同源：跳次是「过程线上的采样时刻」，
# 不是等间距切分。两边必须一致，否则「水位 1.0 的那一跳」和「峰值上报
# 那一跳」会对不上。
DEFAULT_SAMPLE_MIN = (12, 32, 52, 70, 90, 115, 150)

# 封路通告的表格行：| 生效时间 | 2026-10-29 08:30 |
RE_EFFECTIVE = re.compile(r"\|\s*生效时间\s*\|\s*(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2})\s*\|")
RE_DOC_NO = re.compile(r"\|\s*文件编号\s*\|\s*([A-Za-z0-9\-]+)\s*\|")
# 资源台账里写着「跨区调用需 40 分钟」——这个数不是拍的，直接读台账
RE_TRANSFER_DELAY = re.compile(r"跨区调用需\s*(\d+)\s*分钟")
# 作业指引 ZY-DRN-2026-003 的两条硬时限，同样是「读出来的」，不是拍的
RE_RESPONSE_LIMIT = re.compile(r"接工单后\s*(\d+)\s*分钟内到场")
RE_RESTORE_LIMIT = re.compile(r"积水消退后\s*(\d+)\s*小时内恢复通行")


def read_csv(path: Path) -> list[dict[str, str]]:
    # 台账带 BOM，utf-8-sig 才能让首列名对上
    with path.open(encoding="utf-8-sig", newline="") as fh:
        return list(csv.DictReader(fh))


def parse_dt(text: str) -> datetime:
    return datetime.fromisoformat(text.strip())


def pick_event(orders: list[dict[str, str]]) -> dict[str, str]:
    """挑出「本次演示事件」的那条积水工单。

    判据：order_type=waterlogging 且 severity=high。场景里只有兴华路一条
    满足；用它反推事件日期，比另开一个常量可靠——改台账日期时不用同步改脚本。
    """
    hits = [
        o
        for o in orders
        if o.get("order_type") == "waterlogging" and o.get("severity") == "high"
    ]
    if not hits:
        raise SystemExit("work_orders.csv 里找不到 waterlogging/high 工单，无法定事件日期")
    return min(hits, key=lambda o: o["reported_at"])


def main() -> int:
    orders_path = SCENE_DIR / "work_orders.csv"
    alerts_path = SCENE_DIR / "weather_alerts.csv"
    closure_path = SCENE_DIR / "notice_road_closure.md"
    for p in (orders_path, alerts_path):
        if not p.exists():
            print(f"缺台账：{p}", file=sys.stderr)
            return 2

    orders = read_csv(orders_path)
    alerts = read_csv(alerts_path)

    main_order = pick_event(orders)
    event_date = main_order["reported_at"][:10]

    # 事件零点：当天该片区的 active 预警的 valid_from（含时区）
    day_alerts = [
        a
        for a in alerts
        if a.get("status") == "active" and a.get("valid_from", "").startswith(event_date)
    ]
    if not day_alerts:
        print(f"{event_date} 没有 active 预警，无法定事件零点", file=sys.stderr)
        return 2
    alert = max(day_alerts, key=lambda a: float(a.get("rainfall_mm_3h") or 0))
    t0 = parse_dt(alert["valid_from"])
    issued = parse_dt(alert["issued_at"])
    expires = parse_dt(alert["valid_to"])

    # 当天上报的工单（排除事件前的历史单，如 10-26 的泵组故障）
    day_orders = sorted(
        [o for o in orders if o["reported_at"].startswith(event_date)],
        key=lambda o: o["reported_at"],
    )
    first_report = parse_dt(day_orders[0]["reported_at"]) if day_orders else None

    # 封路通告：半幅（施工占道）→ 全幅（积水研判）
    closure_effective = None
    closure_doc = None
    if closure_path.exists():
        text = closure_path.read_text(encoding="utf-8")
        m = RE_EFFECTIVE.search(text)
        if m:
            closure_effective = datetime.strptime(m.group(1), "%Y-%m-%d %H:%M").replace(
                tzinfo=t0.tzinfo
            )
        d = RE_DOC_NO.search(text)
        if d:
            closure_doc = d.group(1)

    # 泵组故障（事件前遗留）：跨日单，单独找
    pump_fault = next((o for o in orders if o.get("order_type") == "pump_fault"), None)

    minutes = list(DEFAULT_SAMPLE_MIN)
    hydro_path = ROOT / "out" / "map2d" / "hydro_curve.json"
    if hydro_path.exists():
        try:
            hm = json.loads(hydro_path.read_text(encoding="utf-8")).get("sample_minutes")
            if isinstance(hm, list) and len(hm) == 7:
                minutes = [int(x) for x in hm]
        except (ValueError, OSError):
            pass

    def clock(minute: int) -> str:
        return (t0 + timedelta(minutes=minute)).strftime("%H:%M")

    def ts(minute: int) -> datetime:
        return t0 + timedelta(minutes=minute)

    # 台账时刻换算成「事件后第几分钟」，用于逐跳比较
    m_closure = int((closure_effective - t0).total_seconds() // 60) if closure_effective else None
    m_report = int((first_report - t0).total_seconds() // 60) if first_report else None

    # ---- 增援泵到场时刻：唯一能算的「人到场」 ----
    # 台账里没有任何「到场时刻」字段（work_orders 只有 reported_at）。但资源台账
    # 给了跨区调用时延，缺口告警时刻又落在确定的跳次上 → 两者相加就能算出泵何时
    # 到位。这比往台账里塞一个编造的 arrived_at 干净：起算点和时延都能指出出处。
    transfer_delay = None
    res_path = SCENE_DIR / "resource_inventory.csv"
    if res_path.exists():
        for row in read_csv(res_path):
            m = RE_TRANSFER_DELAY.search(row.get("capacity_note") or "")
            if m:
                transfer_delay = int(m.group(1))
                break
    ALARM_IDX = 3
    crew_idx = None
    crew_at_min = None
    crew_by_transfer = None
    if transfer_delay is not None:
        crew_by_transfer = minutes[ALARM_IDX] + transfer_delay

    # ---- 作业指引里的两条硬时限 ----
    # ZY-DRN-2026-003 是规范不是记录，但它规定了「多久之内必须做到」，
    # 于是「最晚时刻」是可以算出来的：报案 + 30 分钟、退水 + 2 小时。
    # 这跟编一个「实际到场时刻」不是一回事——前者是制度约束，后者是伪造记录。
    response_limit = None
    restore_limit_h = None
    proc_path = SCENE_DIR / "procedure_waterlogging.md"
    if proc_path.exists():
        text = proc_path.read_text(encoding="utf-8")
        m = RE_RESPONSE_LIMIT.search(text)
        if m:
            response_limit = int(m.group(1))
        m = RE_RESTORE_LIMIT.search(text)
        if m:
            restore_limit_h = int(m.group(1))

    # 到场时刻取两条依据里更晚的那个：跨区时延与到场时限必须同时满足。
    crew_by_rule = (m_report + response_limit) if (m_report is not None and response_limit is not None) else None
    candidates = [v for v in (crew_by_transfer, crew_by_rule) if v is not None]
    if candidates:
        crew_at_min = max(candidates)
        crew_idx = next((k for k, m0 in enumerate(minutes) if m0 >= crew_at_min), None)

    # 退水不等于能通行：作业指引 4.1 要求「积水消退后 2 小时内恢复通行」，
    # 这给的是一个上界（最早可恢复的时刻），不是「已经恢复」。所以标题写「待复核」
    # 并附上这个上界——把制度约束算出来的时间，和实际解封时间分开说。
    restore_clock = None
    if restore_limit_h is not None:
        restore_clock = clock(minutes[-1] + restore_limit_h * 60)
    last_title = (
        f"退水完成：积水退至阈值以下，按指引 {restore_clock} 前恢复通行"
        if restore_clock
        else "退水完成：积水退至阈值以下，待通行复核"
    )
    titles = [
        "降雨持续：兴华路低洼段开始积水",
        "联合封控：道路中心与交警 08:30 起封半幅，另半幅通行",
        "积水上涨：淤积雨水口排水不足，半幅封闭维持",
        # 「3 台损 1」是台账口径，但看的人要知道的是「少了多少能力」。
        # 1200 → 800，缺口 400，正好等于 1 台移动泵（400 m³/h）——一条能算通的因果。
        "泵力缺口：抽排能力从 1200 降到 800 m³/h，缺口 400",
        "峰值上报：核实积水 30cm，扩大为全幅封闭",
        "疏通见效：清理淤积雨水口、泵站全开，积水回落",
        last_title,
    ]

    jumps = []
    for idx, minute in enumerate(minutes):
        now = ts(minute)
        # 半幅：施工占道，有通告时刻；全幅：积水研判后扩大，锚在首次上报
        if m_closure is not None and minute < m_closure:
            block_level = 0
        elif m_report is not None and minute >= m_report:
            block_level = 2
        elif m_closure is not None:
            block_level = 1
        else:
            block_level = 0

        dispatch = m_closure is not None and minute >= m_closure
        # 雨水口淤积是事件前就在册的状态（09-30 巡检），因此首跳即为 True；
        # 疏通解除时刻台账没有，按第 6 跳（疏通动作）起置 False。
        clog = idx < 5
        # 资源缺口：积水逼近峰值时触发（推演）；第 6 跳调泵增援到位后解除，
        # 与「疏通见效」同一时点——缺口靠泵补上，不是自己消失的。
        alarm = ALARM_IDX <= idx < 5
        # 增援泵到场：缺口告警时刻 + 台账跨区时延，落在哪一跳就是哪一跳。
        # 原来写死 idx>=4，等于让泵在告警后 20 分钟就到——台账说跨区要 40 分钟，
        # 那 20 分钟是不可能到的。
        crew = crew_idx is not None and idx >= crew_idx

        basis = {
            "clog": "ledger" if idx == 0 else ("derived" if idx >= 5 else "ledger"),
            "dispatch": "ledger",
            "blockLevel": "ledger" if block_level in (0, 1, 2) else "derived",
            "alarm": "derived",
            "crew": "derived",
        }

        jumps.append(
            {
                "idx": idx,
                "minute": minute,
                "clock": now.strftime("%H:%M"),
                "title": titles[idx] if idx < len(titles) else "",
                "flags": {
                    "clog": clog,
                    "dispatch": dispatch,
                    "blockLevel": block_level,
                    "alarm": alarm,
                    "crew": crew,
                },
                "basis": basis,
            }
        )

    events = [
        {
            "clock": issued.strftime("%H:%M"),
            "label": f"{alert['level']} 级暴雨预警发布（{alert['rainfall_mm_3h']}mm/3h）",
            "basis": "ledger",
            "source": f"weather_alerts.csv {alert['alert_id']}",
        },
        {
            "clock": t0.strftime("%H:%M"),
            "label": "预警生效，降雨开始（事件零点）",
            "basis": "ledger",
            "source": f"weather_alerts.csv {alert['alert_id']} valid_from",
        },
    ]
    if closure_effective:
        events.append(
            {
                "clock": closure_effective.strftime("%H:%M"),
                "label": "兴华路半幅封闭生效（施工占道，道路中心 + 交警联合）",
                "basis": "ledger",
                "source": f"notice_road_closure.md {closure_doc or ''}".strip(),
            }
        )
    for o in day_orders:
        events.append(
            {
                "clock": parse_dt(o["reported_at"]).strftime("%H:%M"),
                "label": f"工单上报：{o['description']}（{o['order_id']}）",
                "basis": "ledger",
                "source": "work_orders.csv",
            }
        )
    if pump_fault:
        events.append(
            {
                "clock": parse_dt(pump_fault["reported_at"]).strftime("%m-%d %H:%M"),
                "label": f"遗留故障：{pump_fault['description']}（事件前已报修，未闭环）",
                "basis": "ledger",
                "source": f"work_orders.csv {pump_fault['order_id']}",
            }
        )
    events.append(
        {
            "clock": expires.strftime("%H:%M"),
            "label": "预警到期解除",
            "basis": "ledger",
            "source": f"weather_alerts.csv {alert['alert_id']} valid_to",
        }
    )
    events.sort(key=lambda e: e["clock"])

    # 每个「推演」开关是怎么算出来的，逐条写清。评委会问「你凭什么说是这个时刻」，
    # 答案要能落到一个数 or 一条台账，而不是「剧本这么写的」。
    derivation = {
        "clog": "状态取自 drainage_facilities.csv（DF-IN-101 state=clogged，09-30 巡检落叶淤积）；"
                "解除时刻台账无记录，按第 6 跳「疏通见效」置否。",
        "alarm": f"判定依据：DF-PS-01 三台机组故障一台（fault_pending_repair）+ 本地存量低于需求。"
                 f"触发时刻无台账，取积水逼近峰值的第 {ALARM_IDX + 1} 跳（{clock(minutes[ALARM_IDX])}）。",
        "crew": (
            "台账无「到场时刻」字段，但两条独立依据同时可用："
            f"① 作业指引 ZY-DRN-2026-003 §1.1「接工单后 {response_limit} 分钟内到场」"
            f"→ 报案 {clock(m_report)} + {response_limit} 分 = {clock(crew_by_rule)}；"
            f"② 资源台账「跨区调用需 {transfer_delay} 分钟」"
            f"→ 告警 {clock(minutes[ALARM_IDX])} + {transfer_delay} 分 = {clock(crew_by_transfer)}。"
            f"取两者较晚者 {clock(crew_at_min)}（落在第 {crew_idx + 1} 跳 {clock(minutes[crew_idx])}）。"
            if (response_limit is not None and m_report is not None and crew_by_rule is not None
                and transfer_delay is not None and crew_by_transfer is not None
                and crew_at_min is not None and crew_idx is not None)
            else "台账无「到场时刻」字段，且缺少可用的时限依据，无法推算。"
        ),
    }

    # 台账本身缺哪些字段——显式列出，而不是用编造的值把洞填上。
    data_gaps = [
        {
            "field": "工单/资源到场时刻",
            "missing": "work_orders.csv 只有 reported_at（报案），无 dispatched_at / arrived_at",
            "workaround": (
                f"由两条时限约束取较晚者推算：作业指引 §1.1 到场时限 {response_limit} 分钟、"
                f"资源台账跨区时延 {transfer_delay} 分钟"
                if response_limit is not None and transfer_delay is not None
                else "缺时限依据，无法推算"
            ),
        },
        {
            "field": "疏通/清掏完成时刻",
            "missing": "无处置过程记录：procedure_waterlogging.md 是作业规范（只给时限，不含时刻），"
                       "工单 §4.2 要求附「作业时长」但台账无该字段",
            "workaround": "按第 6 跳「疏通见效」置否，属推演；恢复通行只能给上界（指引 §4.1）",
        },
        {
            "field": "资源缺口告警触发时刻",
            "missing": "无告警记录表；泵组故障单只有报案时间（10-26，事件前）",
            "workaround": "按积水逼近峰值的跳次触发，属推演",
        },
    ]

    # 制度约束算出来的「最晚时刻」与「实际时刻」必须分开呈现——
    # 一个是规范要求，一个是记录，混在一起就成了拿规范冒充事实。
    limits = {
        "response_min": response_limit,
        "restore_after_h": restore_limit_h,
        "restore_by_clock": restore_clock,
        "source": "procedure_waterlogging.md ZY-DRN-2026-003 §1.1 / §4.1",
    }
    cross_checks = {
        "crew_by_rule_clock": clock(crew_by_rule) if crew_by_rule is not None else None,
        "crew_by_transfer_clock": clock(crew_by_transfer) if crew_by_transfer is not None else None,
        "crew_resolved_clock": clock(crew_at_min) if crew_at_min is not None else None,
        "agree": (crew_by_rule == crew_by_transfer) if (crew_by_rule is not None and crew_by_transfer is not None) else None,
    }

    # 按「开关」而不是「跳次×开关」计数：同一个开关在不同跳次是同一份依据，
    # 按格子数算会让人误以为台账覆盖度是 19/35，实际只有 5 个开关要交代。
    # 分三档，因为 clog 属于「状态有据、解除时刻推演」——只报有据会掩盖推演，
    # 只报推演又抹掉台账，两种都不诚实。
    seen = {f: set() for j in jumps for f in j["basis"]}
    for j in jumps:
        for f, b in j["basis"].items():
            seen[f].add(b)
    ledger_flags = sorted(f for f, bs in seen.items() if bs == {"ledger"})
    mixed_flags = sorted(f for f, bs in seen.items() if bs == {"ledger", "derived"})
    derived_flags = sorted(f for f, bs in seen.items() if bs == {"derived"})

    level_cn = {"blue": "蓝色", "yellow": "黄色", "orange": "橙色", "red": "红色"}
    level_text = level_cn.get(str(alert["level"]).lower(), alert["level"])

    payload = {
        "schema": "map2d-event-timeline-1",
        "note": "由台账反推的 7 跳时标与开关；basis=ledger 有出处、basis=derived 为推演",
        "event_date": event_date,
        "event_start": t0.isoformat(),
        "alert_id": alert["alert_id"],
        "alert_level": alert["level"],
        "alert_level_cn": level_text,
        "rainfall_mm_3h": float(alert["rainfall_mm_3h"]),
        "closure_notice": closure_doc,
        "closure_effective_min": m_closure,
        "first_report_min": m_report,
        "flag_stats": {
            "ledger_flags": ledger_flags,
            "mixed_flags": mixed_flags,
            "derived_flags": derived_flags,
        },
        "derivation": derivation,
        "data_gaps": data_gaps,
        "limits": limits,
        "cross_checks": cross_checks,
        "crew_at_clock": clock(crew_at_min) if crew_at_min is not None else None,
        "transfer_delay_min": transfer_delay,
        "jumps": jumps,
        "events": events,
    }

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"written: {OUT}")
    print(f"  事件日期 {event_date}  零点 {t0.strftime('%H:%M')}  预警 {alert['alert_id']}")
    print(f"  半幅封闭 +{m_closure}min  首次上报 +{m_report}min")
    print(f"  台账有据：{'/'.join(ledger_flags)}")
    print(f"  部分推演：{'/'.join(mixed_flags) or '无'}")
    print(f"  全为推演：{'/'.join(derived_flags) or '无'}")
    for j in jumps:
        f = j["flags"]
        print(
            f"  {j['clock']}  clog={int(f['clog'])} dispatch={int(f['dispatch'])} "
            f"block=L{f['blockLevel']} alarm={int(f['alarm'])} crew={int(f['crew'])}  {j['title']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
