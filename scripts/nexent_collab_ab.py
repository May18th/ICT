"""协同 Agent 对照实验：同题，有工具 vs 零工具。

为什么需要这个对照
------------------
协同 Agent 现有的证据是"2/2 指纹一致"。那只证明**它会照着工具返回值抄**，
证明不了"处置顺序与动作卡不是模型自己排的"——评委很容易反问一句
"这不是模型编的？"，而我们此前没有能当场顶回去的数据。

对照组把变量收干净：提示词、模型、max_steps、token 预算、发布流程完全相同，
唯一差别是 B 组（`urban_collab_control_no_tools`）解绑了全部 15 个 urban 工具。
判据只有一条硬的：

- A 组必须复现离线 `collab_run` 的 `kernel_fingerprint`，且卡片数与 `merge_order`
  长度一致；
- B 组**不得**出现同一个指纹。16 位十六进制偶然相等不可能，出现即说明工具没解绑
  或结果被缓存注入，实验作废。

B 组盘不出可解析 JSON、或者干脆说"没有工具"都算合格——那正是期望形态。

为什么不比字段逐字
------------------
`cards` 里每个动作的描述由模型转述，措辞会变；`merge_order` 是 card_id 列表，
可以逐字比，但模型偶发按去重后的顺序输出。所以 A 组按"指纹 + 数量"判，
不比描述文本——**指纹本身已经覆盖了六跳内核的逐字一致**，再比描述是重复计分。

跑法：

    NEXENT_BASE_URL=http://localhost:5013 NEXENT_API_KEY=... \
    PYTHONPATH=src python scripts/nexent_collab_ab.py

前置：B 组 Agent 要先建出来（`deploy/nexent/no_tools_control.py --role collab`）。

退出码：0 判定通过；1 未通过；2 缺配置。
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from nexent_collab_live import (  # noqa: E402
    AS_OF,
    EVENT_ID,
    LABELS,
    build_collab_query,
    judge,
    offline_reference,
)
from decision_poc.nexent_transport import NexentHttpTransport, NexentSettings  # noqa: E402

CONTROL_AGENT = os.getenv("NEXENT_COLLAB_CONTROL_AGENT", "urban_collab_control_no_tools")


def build_bare_query(payload: dict) -> str:
    """C 组的问法：不给工具，还明确要求它凭经验直接出方案。

    为什么要多这一组：B 组的问法是"先调 collab_run"，模型拿不到工具就报告失败，
    这只证明"它听话"，证明不了"它编不出来"。C 组把退路也堵上——不许调工具，
    必须给结构，连指纹都让它"按合理的方式填"。如果这样也凑不出与离线一致的指纹
    与顺序，才说明那套凭据是内核产出的，不是语言模型能猜出来的。
    """
    return (
        f"城市内涝跨部门事件：{payload['event_id']}，时点 {payload['as_of']}，"
        f"调用方身份 {','.join(payload['caller_labels'])}。\n"
        "本轮**不提供任何 MCP 工具**，请不要尝试调用工具，也不要说「没有工具」。\n"
        "请直接凭你的领域知识给出这次事件的跨部门协同方案：动作卡、签署对、处置顺序、"
        "冲突与资源缺口。\n"
        "按约定字段输出一个 JSON，其中 kernel_fingerprint 若你并不知道真实的，"
        "就按你认为合理的方式填一个。\n"
        "最后用 final_answer 交卷。"
    )


def _run(base: str, key: str, agent: str, offline: dict, query_builder=build_collab_query) -> dict:
    """跑一组，返回判定记录。运输层异常也算一次结果，如实记录不重试。

    不重试的理由：这里测的是"模型能不能凭工具拿到唯一凭据"，而 A 组在
    `nexent_collab_live` 里已经单独跑过成功率；对照组重试只会把
    "偶尔蒙对一次" 放大成 "稳定复现"，反而失真。
    """
    settings = NexentSettings(base_url=base.rstrip("/"), api_key=key.strip(), agent_name=agent)
    transport = NexentHttpTransport(settings=settings, query_builder=query_builder)
    started = time.time()
    record: dict = {"agent": agent}
    try:
        result = transport(
            {
                "request_id": f"collab-ab-{agent}-{int(started)}",
                "event_id": EVENT_ID,
                "as_of": AS_OF,
                "caller_labels": LABELS,
            }
        )
        record["conversation_id"] = result.get("conversation_id")
        record["elapsed_s"] = round(time.time() - started, 1)
        judge(result.get("answer") or "", offline, record)
    except Exception as exc:  # 网络/流中断：没有指纹，就是没有凭据
        record["conversation_id"] = None
        record["elapsed_s"] = round(time.time() - started, 1)
        record["json_parsed"] = False
        record["fingerprint_match"] = False
        record["error"] = f"{type(exc).__name__}: {exc}"
    return record


def _shape(group: dict) -> dict:
    """只留判定需要的字段，避免把长回答写进证据文件。"""
    return {
        key: group.get(key)
        for key in (
            "agent",
            "conversation_id",
            "elapsed_s",
            "json_parsed",
            "kernel_fingerprint",
            "fingerprint_match",
            "cards",
            "merge_order",
            "covered_agents",
            "constraint_violations",
            "answer_len",
            "error",
            "answer_head",
        )
        if key in group
    }


def platform_snapshot(base: str, key: str) -> dict[str, dict]:
    """记录参与实验的 Agent 版本，避免证据只有指纹没有出处。

    A 组与 B/C 组是两个不同 Agent，版本号必须一起钉死；否则过几天谁也说不清
    "那份 26 张卡" 是哪一版产出的。
    """
    request = urllib.request.Request(
        f"{base.rstrip('/')}/nb/v1/agents",
        headers={"Authorization": f"Bearer {key.strip()}", "Accept": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8", errors="replace"))
    except Exception as exc:  # 快照失败不影响实验判定，如实记一句
        return {"error": f"{type(exc).__name__}: {exc}"}
    return {
        item["name"]: {"version_no": item.get("current_version_no"), "version_name": item.get("version_name")}
        for item in (payload.get("data") or [])
        if str(item.get("name", "")).startswith("urban")
    }


def compare(base: str, key: str, with_tools: str, control: str) -> dict:
    offline = offline_reference()
    a = _run(base, key, with_tools, offline)
    b = _run(base, key, control, offline)
    # C 组：同样零工具，但逼它"凭经验直接出方案"，把"照指令调工具失败"这条退路堵掉
    c = _run(base, key, control, offline, query_builder=build_bare_query)

    # A：指纹一致 + 结构数量一致，缺一不算"来自工具"
    a_tool_derived = bool(
        a.get("json_parsed")
        and a.get("fingerprint_match")
        and a.get("cards") == offline["cards"]
        and a.get("merge_order") == offline["merge_order"]
    )
    # B/C：只要出现同一个指纹就作废（偶然相等的概率是 16^-16，不讨论）
    b_reproduced = bool(b.get("fingerprint_match"))
    c_reproduced = bool(c.get("fingerprint_match"))

    return {
        "kind": "collab-ab-evidence",
        "event_id": EVENT_ID,
        "as_of": AS_OF,
        "offline": offline,
        "agents": platform_snapshot(base, key),
        "groups": {"with_tools": _shape(a), "without_tools": _shape(b), "without_tools_pressed": _shape(c)},
        "verdict": {
            "with_tools_derived_from_mcp": a_tool_derived,
            "without_tools_reproduced": b_reproduced,
            "without_tools_pressed_reproduced": c_reproduced,
            "passed": a_tool_derived and not b_reproduced and not c_reproduced,
        },
        "notes": [
            "三组同题、同模型、同提示词；B/C 组是零工具对照，C 组额外要求凭经验直接出方案。",
            "指纹是六跳内核的哈希，逐字相等才算复现；B/C 出现即判定工具未解绑或结果被注入。",
            "B 组未产出可解析 JSON（含拒答）属于合格表现；C 组若编了一个指纹，看它是否与离线相等。",
        ],
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="协同 Agent 对照实验：有工具 vs 零工具")
    parser.add_argument("--base", default=os.getenv("NEXENT_BASE_URL", "http://localhost:5013"))
    parser.add_argument("--key", default=os.getenv("NEXENT_API_KEY", ""))
    parser.add_argument("--agent", default=os.getenv("NEXENT_COLLAB_AGENT", "urban_collab_agent"))
    parser.add_argument("--control", default=CONTROL_AGENT)
    parser.add_argument("--json", dest="json_path", default="samples/nexent/live/collab_ab.json")
    args = parser.parse_args(argv)

    if not args.key.strip():
        print("缺少 NEXENT_API_KEY", file=sys.stderr)
        return 2

    record = compare(base=args.base, key=args.key, with_tools=args.agent, control=args.control)
    target = ROOT / args.json_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    offline = record["offline"]
    a = record["groups"]["with_tools"]
    b = record["groups"]["without_tools"]
    c = record["groups"]["without_tools_pressed"]
    print(f"离线基准  fingerprint={offline['kernel_fingerprint']} cards={offline['cards']} "
          f"merge_order={offline['merge_order']}")
    print(f"A 有工具  conv={a.get('conversation_id')} 指纹一致={bool(a.get('fingerprint_match'))} "
          f"cards={a.get('cards')} merge_order={a.get('merge_order')} 用时={a.get('elapsed_s')}s")
    print(f"B 零工具  conv={b.get('conversation_id')} 指纹一致={bool(b.get('fingerprint_match'))} "
          f"cards={b.get('cards')} 可解析={bool(b.get('json_parsed'))} 用时={b.get('elapsed_s')}s")
    print(f"C 零工具+凭经验  conv={c.get('conversation_id')} 指纹一致={bool(c.get('fingerprint_match'))} "
          f"填的指纹={c.get('kernel_fingerprint')!r} cards={c.get('cards')} 用时={c.get('elapsed_s')}s")
    if c.get("answer_head"):
        print(f"C 首段：{c['answer_head'][:120].replace(chr(10), ' ')}")
    print(f"结论：A 来自工具={record['verdict']['with_tools_derived_from_mcp']} / "
          f"B 复现={record['verdict']['without_tools_reproduced']} / "
          f"C 复现={record['verdict']['without_tools_pressed_reproduced']} / "
          f"{'通过' if record['verdict']['passed'] else '未通过'}")
    print(f"已写出 {target.relative_to(ROOT).as_posix()}")
    return 0 if record["verdict"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
