"""建一个「同提示词、零 MCP 工具」的对照 Agent，用来验证结论确实来自工具。

为什么需要它
------------
主链路 8/8 只能说明"端到端一致"，不能排除"模型本来就会答"。评审最常问的一句是
"你怎么证明这 8 项是靠 MCP 工具拿到的，不是模型自己编的"。对照组是唯一干净的答法：
提示词、模型、预算、发布流程全部相同，**只把 14 个 urban 工具解绑**，再问同一组问题。

期望结果
--------
对照组拿不到 `event_deduce` 的六跳事实，8 项一致性应当**不成立**（答错、字段缺失或拒答）。
如果对照组也能 8/8，说明这套验收测的是模型先验而不是工具能力，主链路的结论要作废重做。

用法（宿主机，先 `docker cp` 两个文件进容器）：

    docker cp deploy/nexent/platform_client.py nexent-config:/tmp/platform_client.py
    docker cp deploy/nexent/setup_nexent.py    nexent-config:/tmp/setup_nexent.py
    docker cp deploy/nexent/no_tools_control.py nexent-config:/tmp/no_tools_control.py
    docker exec nexent-config sh -c "cd /opt/backend && .venv/bin/python /tmp/no_tools_control.py"

幂等：同名 Agent 已存在时只回写草稿、重新解绑并发布，不会重复创建。
"""

from __future__ import annotations

import os
import sys

from setup_nexent import (
    CONSTRAINT_PROMPT,
    DUTY_PROMPT,
    MAIN_REQUESTED_OUTPUT_TOKENS,
    MODEL_IDS,
    Platform,
)

CONTROL_NAME = os.getenv("NEXENT_CONTROL_AGENT", "urban_control_no_tools")


def main() -> int:
    platform = Platform()

    status, agents = platform.call("/api/agent/list")
    agent = None
    for item in agents if isinstance(agents, list) else []:
        if item.get("name") == CONTROL_NAME:
            agent = item
            break

    payload = {
        "name": CONTROL_NAME,
        "display_name": "对照：无 MCP 工具的城市内涝智能体",
        "description": "实验对照组。提示词/模型与主 Agent 完全相同，仅解绑 15 个 MCP 工具，用于证明 8/8 来自工具而非模型先验",
        "business_description": "不作为业务入口，仅供验收取证",
        "author": "UrbanEvolve",
        "model_ids": MODEL_IDS,
        "max_steps": 20,
        "duty_prompt": DUTY_PROMPT,
        "constraint_prompt": CONSTRAINT_PROMPT,
        "enable_context_manager": False,
        "enable_protocol_repair_retry": True,
        "requested_output_tokens": MAIN_REQUESTED_OUTPUT_TOKENS,
    }
    if agent is None:
        status, body = platform.call("/api/agent/update", method="POST", payload=payload)
        print(f"[control] create: {status} {str(body)[:200]}")
        agent = next(
            (a for a in (platform.call("/api/agent/list")[1] or []) if a.get("name") == CONTROL_NAME),
            None,
        )
    else:
        payload["agent_id"] = int(agent["agent_id"])
        status, body = platform.call("/api/agent/update", method="POST", payload=payload)
        print(f"[control] update(draft): {status} {str(body)[:200]}")

    if agent is None:
        print("[control] 创建失败")
        return 1

    agent_id = int(agent["agent_id"])
    print(f"[control] id={agent_id} name={CONTROL_NAME}")

    # 零工具：显式解绑 14 个 urban 工具（attach_tools_for 的 names 传空集即全部 detach）
    platform.attach_tools_for(agent_id, set())

    status, body = platform.call(
        f"/api/agent/{agent_id}/publish",
        method="POST",
        payload={"version_name": "control-no-tools-v1", "release_note": "零 MCP 工具对照组"},
    )
    print(f"[control] publish: {status} {str(body)[:200]}")

    status, listing = platform.call(f"/api/tool/list?agent_id={agent_id}&version_no=0")
    names = [t.get("origin_name") or t.get("name") for t in listing] if isinstance(listing, list) else []
    urban_left = sorted(
        set(names)
        & {
            "semantic_query", "twin_neighbors", "evidence_locate", "claim_trace",
            "provenance_verify", "event_deduce", "counterfactual_run", "conflict_check",
            "allocation_plan", "scene_render", "scene_status", "scene_compare",
            "asset_lookup", "collab_run",
        }
    )
    print(f"[control] 剩余 urban 工具: {urban_left or '无'}")
    print(f"CONTROL_AGENT_ID={agent_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
