"""在真实 Nexent 平台上走一遍分层记忆：写入 → 列表 → 召回 → 上下文 → 隔离 → 清理。

为什么必须在 `nexent-config` 容器里跑
------------------------------------
记忆接口挂在平台管理 API（5010，`/memory/...`），鉴权是 JWT HS256，签名密钥只存在于
`nexent-config` 容器环境。北向 5013 上没有这些路由（实测 404），别在那儿试。

这条脚本补的是「分层记忆未在平台侧验证」那一格：离线侧五个步骤早已跑通
（`docs/reports/nexent-memory-walkthrough.md`），缺的是平台上真写一条、真召回一次。

三层分工（与平台表结构一一对应）
--------------------------------
- tenant：`/memory/long-term/tenant`，城市口径的稳定规范（预案/阈值/策略台账），写要 ADMIN 角色；
- user：`/memory/long-term/user`，指挥员偏好；
- agent：`/memory/records`，当前工单推演。**这张表只收 `memory_type=short_term`**（库上有约束
  `ck_memory_records_agent_short_term`：`layer = 'agent' AND memory_type = 'short_term'`），
  且要带 `conversation_id` 才能按会话隔离。长期记忆不是这张表。

本脚本只写 agent 层并当场清理，另外两层做只读探测——写 tenant/user 会改变所有对话看到的
长期记忆，属于平台状态变更，不在无人值守脚本里做。

用法（宿主机）
--------------
    docker cp deploy/nexent/platform_client.py      nexent-config:/tmp/platform_client.py
    docker cp deploy/nexent/memory_roundtrip.py     nexent-config:/tmp/memory_roundtrip.py
    docker exec nexent-config sh -c "cd /opt/backend && .venv/bin/python /tmp/memory_roundtrip.py \
        --json /tmp/memory_roundtrip.json"
    docker cp nexent-config:/tmp/memory_roundtrip.json samples/nexent/live/memory_roundtrip.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import uuid

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from urllib.parse import quote  # noqa: E402

from platform_client import PlatformClient, config_api  # noqa: E402

PROBE_TAG = "mem-probe"
PROBE_CONTENT = (
    "场景：A-03 兴华路积水 30cm + B-01 缺口 0.667；"
    "在该场景下 flood-response-v1.2 的分配偏差为 0.0，"
    "跨区调 2 台移动泵后 A-03 缺口归零、B-01 未满足 2 项。"
)
PROBE_QUERY = "A-03 B-01 抽排缺口 分配策略 偏差"
PROBE_CONV = "mem-probe-20261122"


def _mint_for(sub: str, expires_in: int = 600) -> str:
    """签一个指定 sub 的 JWT，用来验证跨用户隔离。"""
    import jwt

    secret = os.getenv("SUPABASE_JWT_SECRET") or os.getenv("JWT_SECRET")
    if not secret:
        raise RuntimeError("容器环境里没有 JWT_SECRET")
    now = int(time.time())
    return jwt.encode(
        {"sub": sub, "exp": now + expires_in, "iat": now, "role": "authenticated"},
        secret,
        algorithm="HS256",
    )


class AltUserClient(PlatformClient):
    """换成另一个 user_id 的客户端；只用于隔离验证。"""

    def __init__(self, sub: str, base_url: str | None = None) -> None:
        self.base_url = base_url or config_api()
        self.token = _mint_for(sub)


def _ids(body) -> list:
    if isinstance(body, dict):
        items = body.get("items") or body.get("data") or []
        if isinstance(items, list):
            return [item.get("memory_id") or item.get("id") for item in items if isinstance(item, dict)]
    return []


def _has_id(client: PlatformClient, memory_id) -> bool:
    status, body = client.call("/memory/records?limit=50")
    return status == 200 and memory_id in _ids(body)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="平台分层记忆读写闭环验证")
    ap.add_argument("--json", default="", help="证据 JSON 落盘路径（容器内）")
    ap.add_argument("--keep", action="store_true", help="保留探针记录，不清理")
    args = ap.parse_args(argv)

    client = PlatformClient()
    steps: list[dict] = []

    def record(step: str, status, detail) -> None:
        steps.append({"step": step, "http": status, "detail": detail})
        print(f"[{step}] http={status} {json.dumps(detail, ensure_ascii=False)[:300]}")

    # 0 只读探测：两层长期记忆当前是否有内容（不写）
    for scope in ("tenant", "user"):
        status, body = client.call(f"/memory/long-term/{scope}")
        empty = body.get("empty") if isinstance(body, dict) else None
        record(f"read_long_term_{scope}", status, {"empty": empty})

    # 1 基线：当前 agent 层有多少条
    status, body = client.call("/memory/records?limit=50")
    baseline = _ids(body)
    record("baseline_list", status, {"count": len(baseline)})

    # 2 写入一条 agent 层记忆（idempotency_key 固定，重跑不叠加）
    payload = {
        "layer": "agent",
        "memory_type": "short_term",
        "agent_id": "urban_deduction_agent",
        "conversation_id": PROBE_CONV,
        "content": PROBE_CONTENT,
        "concept_tags": ["urban-drainage", "allocation", PROBE_TAG],
        "idempotency_key": "mem-probe-20261122",
    }
    status, body = client.call("/memory/records", method="POST", payload=payload)
    created = body if isinstance(body, dict) else {}
    memory_id = created.get("memory_id") or created.get("id")
    payload_summary = {k: payload[k] for k in ("layer", "memory_type", "agent_id", "idempotency_key")}
    record("write_agent_record", status, {"memory_id": memory_id, "request": payload_summary})
    if status not in (200, 201) or memory_id is None:
        record("abort", status, {"reason": "写入失败，后续步骤跳过", "body": str(body)[:300]})
        memory_id = None

    # 3 列表确认落库
    if memory_id is not None:
        status, body = client.call("/memory/records?limit=50")
        found = memory_id in _ids(body)
        record("list_after_write", status, {"found": found, "count": len(_ids(body))})

    # 4 召回
    if memory_id is not None:
        status, body = client.call(
            "/memory/records/search",
            method="POST",
            payload={"query": PROBE_QUERY, "layers": ["agent"], "agent_id": "urban_deduction_agent",
                     "top_k": 3, "threshold": 0.3},
        )
        hits = []
        if isinstance(body, dict):
            for item in body.get("items") or []:
                hits.append({
                    "memory_id": item.get("memory_id") or item.get("id"),
                    "score": item.get("score") if "score" in item else item.get("similarity"),
                })
        record("search_recall", status, {"count": len(hits), "hits": hits[:3],
                                         "hit_probe": any(h.get("memory_id") == memory_id for h in hits)})

    # 5 上下文取用（平台真正注入提示词的那一段）
    if memory_id is not None:
        status, body = client.call(
            "/memory/context?query=" + quote(PROBE_QUERY)
            + "&agent_id=urban_deduction_agent&top_k=5&threshold=0.3"
        )
        prompt = (body.get("prompt_text") or "") if isinstance(body, dict) else ""
        blocks = {}
        if isinstance(body, dict):
            for key in ("tenant_long_term", "user_long_term", "agent_short_term", "external"):
                value = body.get(key)
                blocks[key] = len(value) if isinstance(value, list) else None
        record("context_injection", status, {
            "blocks": blocks,
            "prompt_chars": len(prompt),
            "contains_probe": "flood-response-v1.2" in prompt or "B-01" in prompt,
        })

    # 6 跨用户隔离：换一个 sub，同一条记录必须看不到
    if memory_id is not None:
        other = AltUserClient(str(uuid.uuid4()))
        status, body = other.call("/memory/records?limit=50")
        leaked = memory_id in _ids(body)
        record("isolation_other_user", status, {"visible_to_other_user": leaked,
                                                "count_for_other_user": len(_ids(body))})

    # 7 清理
    if memory_id is not None and not args.keep:
        status, body = client.call(f"/memory/records/{memory_id}", method="DELETE")
        record("delete_probe", status, {"memory_id": memory_id})
        status, _ = client.call("/memory/records?limit=50")
        record("list_after_delete", status, {"still_present": _has_id(client, memory_id)})

    ok_steps = {"write_agent_record", "list_after_write", "search_recall",
                "context_injection", "isolation_other_user"}
    passed = all(
        s["http"] in (200, 201)
        for s in steps if s["step"] in ok_steps
    )
    isolation_ok = next((s["detail"]["visible_to_other_user"] is False
                         for s in steps if s["step"] == "isolation_other_user"), None)
    recall_ok = next((s["detail"]["hit_probe"] for s in steps if s["step"] == "search_recall"), None)

    result = {
        "schema_version": "nexent-memory-roundtrip-1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base_url": config_api(),
        "probe": {"content": PROBE_CONTENT, "query": PROBE_QUERY,
                  "idempotency_key": payload["idempotency_key"]},
        "steps": steps,
        "summary": {
            "write_ok": any(s["step"] == "write_agent_record" and s["http"] in (200, 201) for s in steps),
            "listed": next((s["detail"]["found"] for s in steps if s["step"] == "list_after_write"), None),
            "recall_hit": recall_ok,
            "isolation_ok": isolation_ok,
            "all_http_ok": passed,
        },
        "note": "tenant/user 两层只做只读探测：写它们会改变所有对话看到的长期记忆，属平台状态变更。",
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
    print("\n" + json.dumps(result["summary"], ensure_ascii=False))
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
