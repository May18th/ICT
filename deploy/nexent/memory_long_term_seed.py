"""把租户级与用户级长期记忆写进平台，并读回验证。

背景
----
`memory_roundtrip.py` 只验证 agent 层短期记忆（`/memory/records`），tenant / user 两层
当时刻意只做只读探测：写它们会改变**所有对话**看到的长期记忆，属平台状态变更。
这一脚本就是补这两层，写完立刻读回，并落一份证据。

三层分工（与平台表结构一一对应）
--------------------------------
- tenant：`/memory/long-term/tenant/versions`，版本化 Markdown，写要 ADMIN 角色；
- user：`/memory/long-term/user/versions`，同上，按 JWT 的 `sub` 隔离；
- agent：`/memory/records`，短期工单推演，见 `memory_roundtrip.py`。

为什么在 `nexent-config` 容器里跑
--------------------------------
管理 API 在 5010（宿主机未发布端口），鉴权 JWT 的签名密钥只在容器环境里。

回退
----
长期记忆是版本链，不能删只能盖：再 POST 一个新版本即成为 active，旧版本留在链上可查。
`GET /memory/long-term/{scope}/versions` 能看到全部版本与时间。

用法（宿主机）
--------------
    docker cp deploy/nexent/platform_client.py        nexent-config:/tmp/platform_client.py
    docker cp deploy/nexent/memory_long_term_seed.py  nexent-config:/tmp/memory_long_term_seed.py
    docker exec nexent-config sh -c "cd /opt/backend && .venv/bin/python /tmp/memory_long_term_seed.py \\
        --json /tmp/memory_long_term.json"
    docker cp nexent-config:/tmp/memory_long_term.json samples/nexent/live/memory_long_term.json
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from urllib.parse import quote

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from platform_client import PlatformClient, config_api  # noqa: E402

TENANT_CONTENT = """# 城市内涝处置值守规范（成都兴华路片区）

## 适用范围
- 单片区：成都兴华路片区约 3 公里。方案可迁移，标定参数不可迁移。

## 引用纪律
- 条款依据必须落到预案原文，引用带文件名与条款定位；查不到原文的结论不输出。
- 事实类问题走工具（台账查询），条款类问题走检索，两者不混用。

## 数值口径
- 积水深度以台账值为准。峰值 29.3cm 是几何路径估算，对台账 30cm 偏差 −2.4%，不宣称模型精度。
- 资源缺口按“额定能力 × 0.8 保守折算”，属折算口径，不是水力模型测算值。

## 策略变更
- 任何策略调整先过评测门禁（训练集与留出集双集），不达标不上线；上线后保留可回退版本。
"""

USER_CONTENT = """# 值班指挥员偏好

- 先给结论与处置顺序，再给依据；不先铺陈过程。
- 每条结论标责任机构；涉及条款的标原文定位。
- 下穿隧道、医院与学校周边路段优先处置。
- 不接受没有出处的数字，宁可写“待核实”。
- 跨部门动作先说明谁与谁联合发布。
"""

PROBE_QUERY = "积水深度阈值 资源缺口 折算 联合发布"


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="写租户级与用户级长期记忆并读回")
    ap.add_argument("--json", default="", help="证据 JSON 落盘路径（容器内）")
    ap.add_argument("--dry-run", action="store_true", help="只读现状，不写")
    args = ap.parse_args(argv)

    client = PlatformClient()
    steps: list[dict] = []

    def record(step: str, status, detail) -> None:
        steps.append({"step": step, "http": status, "detail": detail})
        print(f"[{step}] http={status} {json.dumps(detail, ensure_ascii=False)[:320]}")

    plan = (("tenant", TENANT_CONTENT), ("user", USER_CONTENT))
    written: dict[str, int | None] = {}

    # 0 写前现状：两层当前是否有 active 版本
    for scope, _ in plan:
        status, body = client.call(f"/memory/long-term/{scope}")
        empty = body.get("empty") if isinstance(body, dict) else None
        version = (body or {}).get("version") if isinstance(body, dict) else None
        record(f"read_before_{scope}", status, {
            "empty": empty,
            "active_version_id": (version or {}).get("version_id") or (version or {}).get("id"),
        })

    if args.dry_run:
        record("dry_run", None, {"reason": "只读模式，未写入"})
        return 0

    # 1 写两层
    for scope, content in plan:
        status, body = client.call(
            f"/memory/long-term/{scope}/versions",
            method="POST",
            payload={"content": content},
        )
        version = body if isinstance(body, dict) else {}
        version_id = version.get("version_id") or version.get("id")
        written[scope] = version_id
        record(f"write_{scope}", status, {
            "version_id": version_id,
            "chars": len(content),
            "active": version.get("is_active") if "is_active" in version else version.get("active"),
        })

    # 2 读回：active 版本与版本链
    for scope, content in plan:
        status, body = client.call(f"/memory/long-term/{scope}")
        version = (body or {}).get("version") if isinstance(body, dict) else None
        text = (version or {}).get("content") or ""
        record(f"read_after_{scope}", status, {
            "empty": (body or {}).get("empty"),
            "version_id": (version or {}).get("version_id") or (version or {}).get("id"),
            "chars": len(text),
            "content_matches": text.strip() == content.strip(),
        })
        status, body = client.call(f"/memory/long-term/{scope}/versions?limit=20")
        items = (body or {}).get("items") if isinstance(body, dict) else []
        record(f"versions_{scope}", status, {"count": (body or {}).get("count"),
                                            "ids": [i.get("version_id") or i.get("id") for i in (items or [])][:10]})

    # 3 提示词注入：context 接口是否把两层带进 prompt
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
        "has_tenant_text": "引用纪律" in prompt or "值守规范" in prompt,
        "has_user_text": "指挥员偏好" in prompt or "处置顺序" in prompt,
    })

    ok_steps = {f"write_{s}" for s, _ in plan} | {f"read_after_{s}" for s, _ in plan} | {"context_injection"}
    all_ok = all(s["http"] in (200, 201, 204) for s in steps if s["step"] in ok_steps)

    result = {
        "schema_version": "nexent-memory-long-term-1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "base_url": config_api(),
        "scopes": {
            scope: {
                "chars": len(content),
                "version_id": written.get(scope),
                "content": content,
            }
            for scope, content in plan
        },
        "steps": steps,
        "summary": {
            "all_http_ok": all_ok,
            "tenant_written": written.get("tenant") is not None,
            "user_written": written.get("user") is not None,
        },
        "note": ("两层长期记忆是版本链，写后对所有对话生效；回退方式是新写一版覆盖，"
                 "旧版本仍可从 /memory/long-term/{scope}/versions 查到。"),
    }
    text = json.dumps(result, ensure_ascii=False, indent=2)
    if args.json:
        with open(args.json, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text + "\n")
    print("\n" + json.dumps(result["summary"], ensure_ascii=False))
    return 0 if all_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
