"""协同 Agent（urban_collab_agent）真机验证：多次调用，比对四个协同字段与内核指纹。

本脚本的 live 证据范围是平台最终回答中的动作卡、处置顺序、角色覆盖、约束违规和内核指纹。
本地/MCP `collab_run` 生成的 `AgentMessage` 消息信封不纳入本轮 Nexent live 判定，
因此本脚本不宣称线上 A2A 或线上消息交接已经验收。

为什么单独一个脚本而不是复用 nexent_urban
------------------------------------------
主 Agent 校验的是"六跳结论 8 项一致"；协同 Agent 校验的是"四个协同字段与内核指纹逐字一致"。
后者是格式易崩的一环（模型爱加解释、爱发并行工具调用、爱用 final_answer 包装），所以本脚本
**跑多次算成功率**，并把每次的原始回答头部落盘，避免"跑通一次就宣称稳定"。

两种判定入口：
    --attempts 3      真机调用 3 次（慢，约 2-4 分钟/次）
    --reparse 71,73   只重新判定已有会话（不调模型，用于修正解析口径后复核）

用法：
    PYTHONPATH=src NEXENT_BASE_URL=http://localhost:5013 NEXENT_API_KEY=... \
    NEXENT_COLLAB_AGENT=urban_collab_agent \
    uv run --locked python scripts/nexent_collab_live.py --attempts 3 \
        --json samples/nexent/live/collab_live.json

退出码：0 = 至少一次五个字段与指纹均逐字一致，且失败次数已如实记录；1 = 全部失败；2 = 缺配置。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from decision_poc.mcp_server import UrbanToolkit  # noqa: E402
from decision_poc.nexent_transport import NexentHttpTransport, NexentSettings  # noqa: E402

EVENT_ID = "WO-20261029-007"
AS_OF = "2026-10-29T09:40:00+08:00"
LABELS = ["ORG-EMERG"]

# 交卷契约：回答里必须出现的顶层字段，以及卡片只回交这几个键。
# 平台侧按提示词回交，本地 collab_run 的卡片比它多 9 个内部字段，比对前统一投影到这一层。
ANSWER_FIELDS = ("cards", "merge_order", "covered_agents", "constraint_violations")
ANSWER_CARD_KEYS = (
    "action",
    "agent_id",
    "card_id",
    "co_sign_required",
    "depends_on",
    "stage",
    "status",
)


def project_cards(cards: list[Any]) -> list[dict[str, Any]]:
    """把卡片投影到交卷字段，两端用同一函数，避免"多几个内部字段就判假"或"少几个字段还判真"。"""
    return [{key: card.get(key) for key in ANSWER_CARD_KEYS} for card in cards]


def build_collab_query(payload: dict[str, Any]) -> str:
    return (
        f"城市内涝跨部门事件：{payload['event_id']}，时点 {payload['as_of']}，"
        f"调用方身份 {','.join(payload['caller_labels'])}。\n"
        "请先调用 collab_run 拿到协同结果。\n"
        "**最后一步必须用 final_answer 交卷**：写一个 `<code>` 代码块，块内只有 "
        "`final_answer(json.dumps(协同结果对象, ensure_ascii=False))` 这一句；块外不要再写 JSON 正文、不要解释。\n"
        "kernel_fingerprint 必须与工具返回逐字一致，不得新增动作卡、不得改写冲突。"
    )


def offline_reference() -> dict[str, Any]:
    """本地跑一次 collab_run 作为比对基准（不经过平台）。"""
    toolkit = UrbanToolkit()
    result = toolkit.call("collab_run", {"event_id": EVENT_ID, "as_of": AS_OF, "caller_labels": LABELS})
    data = result.data if hasattr(result, "data") else result
    field_values = {
        "cards": list(data.get("cards") or []),
        "merge_order": list(data.get("merge_order") or []),
        "covered_agents": list(data.get("covered_agents") or []),
        "constraint_violations": list(data.get("constraint_violations") or []),
    }
    return {
        "kernel_fingerprint": data.get("kernel_fingerprint"),
        "cards": len(field_values["cards"]),
        "merge_order": len(field_values["merge_order"]),
        "covered_agents": len(field_values["covered_agents"]),
        "constraint_violations": field_values["constraint_violations"],
        "_field_values": field_values,
    }


def public_offline_reference(offline: dict[str, Any]) -> dict[str, Any]:
    """去掉只供当前进程逐字段比较的完整基准，避免证据快照膨胀。"""
    return {key: value for key, value in offline.items() if not key.startswith("_")}


def strip_action_history(text: str) -> str:
    """去掉平台回注的 <completed_action_history> 块，只留下最终回答。"""
    return re.sub(r"<completed_action_history[\s\S]*?</completed_action_history>", "", text)


def _balanced_objects(body: str) -> list[dict[str, Any]]:
    """列出文本里所有平衡的 JSON 对象（按出现顺序）。"""
    found: list[dict[str, Any]] = []
    start = body.find("{")
    while start != -1:
        depth = 0
        in_str = False
        escape = False
        for idx in range(start, len(body)):
            ch = body[idx]
            if in_str:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_str = False
                continue
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    try:
                        found.append(json.loads(body[start : idx + 1]))
                    except json.JSONDecodeError:
                        pass
                    break
        start = body.find("{", start + 1)
    return found


def extract_json(text: str) -> dict[str, Any] | None:
    """从回答里找出协同结果 JSON。

    平台有两种合法外壳，必须都认：
    1) 裸 JSON；
    2) `final_answer` 工具包装（`{"name": "final_answer", "arguments": {"content": "<json 字符串>"}}`）。
    只认"含 kernel_fingerprint 的那一个"，否则会把动作历史里的片段当成结论。
    """
    body = strip_action_history(text)
    body = re.sub(r"```(?:json)?", "", body)
    candidates: list[dict[str, Any]] = []
    for obj in _balanced_objects(body):
        candidates.append(obj)
        args = obj.get("arguments") if isinstance(obj, dict) else None
        if isinstance(args, dict) and isinstance(args.get("content"), str):
            candidates.extend(_balanced_objects(args["content"]))
    for obj in candidates:
        if isinstance(obj, dict) and "kernel_fingerprint" in obj:
            return obj
    return candidates[0] if candidates else None


def judge(answer: str, offline: dict[str, Any], record: dict[str, Any]) -> dict[str, Any]:
    """把一次回答折算成判定结果，写回 record。

    比对前两端先投影到同一层：平台回答只回交卷字段（`ANSWER_CARD_KEYS`），
    本地 `collab_run` 的卡片另有 9 个内部字段（claim_ids/evidence_refs/quantity…）。
    直接整卡相等会让真机回答永远判不过，那是口径问题不是模型问题。
    """
    parsed = extract_json(answer)
    record["answer_len"] = len(answer)
    if parsed is None:
        record["json_parsed"] = False
        record["fingerprint_match"] = False
        record["field_matches"] = {}
        record["shape_match"] = False
        record["mismatches"] = ["json_parse"]
        record["answer_head"] = strip_action_history(answer)[:300]
        return record
    record["json_parsed"] = True
    got_fp = str(parsed.get("kernel_fingerprint") or "")
    record["kernel_fingerprint"] = got_fp
    record["fingerprint_match"] = got_fp == offline["kernel_fingerprint"]
    absent = [name for name in ANSWER_FIELDS + ("kernel_fingerprint",) if name not in parsed]
    got_fields = {
        "cards": project_cards(list(parsed.get("cards") or [])),
        "merge_order": list(parsed.get("merge_order") or []),
        "covered_agents": list(parsed.get("covered_agents") or []),
        "constraint_violations": list(parsed.get("constraint_violations") or []),
    }
    expected_fields = offline.get("_field_values")
    if isinstance(expected_fields, dict):
        expected_fields = dict(expected_fields)
        expected_fields["cards"] = project_cards(list(expected_fields.get("cards") or []))
    else:
        # 旧证据只有数量，不能冒充逐字段比较；让这次判定明确失败。
        expected_fields = {name: offline.get(name) for name in ANSWER_FIELDS}
    record["cards"] = len(got_fields["cards"])
    record["merge_order"] = len(got_fields["merge_order"])
    record["covered_agents"] = len(got_fields["covered_agents"])
    record["constraint_violations"] = got_fields["constraint_violations"]
    record["card_keys"] = list(ANSWER_CARD_KEYS)
    field_matches = {
        field: field not in absent and got_fields[field] == expected_fields.get(field)
        for field in got_fields
    }
    record["field_matches"] = field_matches
    record["shape_match"] = all(field_matches.values())
    record["mismatches"] = [name for name, matched in field_matches.items() if not matched]
    if not record["fingerprint_match"]:
        record["mismatches"].append("kernel_fingerprint")
    if not got_fp:
        record["answer_head"] = strip_action_history(answer)[:300]
    return record


def fetch_conversation(conversation_id: int) -> str:
    """从北向拉某次会话的最终回答（用于 --reparse）。"""
    base = os.getenv("NEXENT_BASE_URL", "http://localhost:5013").rstrip("/")
    key = os.getenv("NEXENT_API_KEY", "").strip()
    request = urllib.request.Request(
        f"{base}/nb/v1/conversations/{conversation_id}",
        headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8", errors="replace"))
    history = (payload.get("data") or {}).get("history") or []
    assistants = [item for item in history if item.get("role") == "assistant"]
    return (assistants[-1].get("content") or "") if assistants else ""


def summarize(attempts: list[dict[str, Any]], args: argparse.Namespace, offline: dict[str, Any]) -> dict[str, Any]:
    ok = [
        a for a in attempts
        if a.get("json_parsed") and a.get("shape_match") and a.get("fingerprint_match")
    ]
    payload: dict[str, Any] = {
        "kind": "collab-live-evidence",
        "event_id": EVENT_ID,
        "as_of": AS_OF,
        "agent_name": args.agent,
        "captured_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "method": (
            "北向 /nb/v1/chat/run 实测；本轮 live 范围只覆盖 cards、merge_order、covered_agents、"
            "constraint_violations 与 kernel_fingerprint。成功判据 = 从最终回答解析出含 kernel_fingerprint "
            "的 JSON，五个字段齐全且与离线投影后逐字一致（卡片只比交卷字段）。平台两种合法外壳"
            "（裸 JSON / final_answer 包装）都认；本地/MCP AgentMessage 消息信封不纳入判定，"
            "不据此宣称线上 A2A。"
        ),
        "answer_contract": {
            "fields": list(ANSWER_FIELDS) + ["kernel_fingerprint"],
            "card_keys": list(ANSWER_CARD_KEYS),
        },
        "offline": public_offline_reference(offline),
        "attempts": attempts,
        "success": bool(ok),
        "success_attempt": ok[0]["attempt"] if ok else None,
        "success_rate": f"{len(ok)}/{len(attempts)}",
    }
    if len(ok) < len(attempts):
        payload["known_limitation"] = (
            f"协同 Agent 输出稳定性：{len(ok)}/{len(attempts)} 次返回可解析 JSON 且五个字段与指纹一致；"
            "失败形态是偶发返回空骨架 JSON（各字段为 null、cards 为空，未调用工具）；"
            "验收需支持重跑与格式容错，不宣称每次 100% 成功"
        )
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="协同 Agent 真机验证（多次调用算成功率）")
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--reparse", default=None, help="逗号分隔的 conversation_id，只重新判定已有会话")
    parser.add_argument("--agent", default=os.getenv("NEXENT_COLLAB_AGENT", "urban_collab_agent"))
    parser.add_argument("--json", dest="json_path", default="samples/nexent/live/collab_live.json")
    args = parser.parse_args(argv)

    missing = [k for k in ("NEXENT_BASE_URL", "NEXENT_API_KEY") if not os.getenv(k)]
    if missing:
        print(f"缺少环境变量：{missing}")
        return 2

    offline = offline_reference()
    print(f"[offline] kernel_fingerprint={offline['kernel_fingerprint']} cards={offline['cards']} "
          f"merge_order={offline['merge_order']} agents={offline['covered_agents']}")

    attempts: list[dict[str, Any]] = []
    if args.reparse:
        for index, raw in enumerate([x for x in args.reparse.split(",") if x.strip()], 1):
            conversation_id = int(raw)
            answer = fetch_conversation(conversation_id)
            record = judge(answer, offline, {"attempt": index, "conversation_id": conversation_id})
            attempts.append(record)
            print(f"[reparse {conversation_id}] " + json.dumps(
                {k: v for k, v in record.items() if k != "answer_head"}, ensure_ascii=False))
    else:
        # `--agent` 优先于环境变量：settings 直接构造，不走 from_env（后者只认 NEXENT_AGENT_NAME，
        # 会让 `--agent` 静默失效——表现为"改了半天 agent 名，跑的还是默认那个"）
        settings = NexentSettings(
            base_url=os.getenv("NEXENT_BASE_URL", "http://localhost:5013").rstrip("/"),
            api_key=os.getenv("NEXENT_API_KEY", "").strip(),
            agent_name=args.agent,
        )
        transport = NexentHttpTransport(settings=settings, query_builder=build_collab_query)
        for index in range(1, args.attempts + 1):
            started = time.time()
            record: dict[str, Any] = {"attempt": index}
            try:
                result = transport(
                    {
                        "request_id": f"collab-live-{index}-{int(started)}",
                        "event_id": EVENT_ID,
                        "as_of": AS_OF,
                        "caller_labels": LABELS,
                    }
                )
                record["conversation_id"] = result.get("conversation_id")
                record["elapsed_s"] = round(time.time() - started, 1)
                judge(result.get("answer") or "", offline, record)
            except Exception as exc:  # 网络/流中断都算一次失败，如实记录
                record["conversation_id"] = None
                record["elapsed_s"] = round(time.time() - started, 1)
                record["json_parsed"] = False
                record["error"] = f"{type(exc).__name__}: {exc}"
            attempts.append(record)
            print(f"[attempt {index}] " + json.dumps(
                {k: v for k, v in record.items() if k != "answer_head"}, ensure_ascii=False))

    payload = summarize(attempts, args, offline)
    out = Path(args.json_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"[collab] 成功率 {payload['success_rate']}，证据写入 {out}")
    return 0 if payload["success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
