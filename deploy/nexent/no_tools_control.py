"""建一个「同提示词、零 MCP 工具」的对照 Agent，用来验证结论确实来自工具。

为什么需要它
------------
端到端一致只能说明"这一轮答对了"，不能排除"模型本来就会答"。评审最常问的一句是
"你怎么证明这些结论是靠 MCP 工具拿到的，不是模型自己编的"。对照组是唯一干净的答法：
提示词、模型、预算、发布流程全部相同，**只把 15 个 urban 工具解绑**，再问同一组问题。

两个角色（`--role`）
--------------------
- `main`（默认）对应主 Agent 的 DUTY_PROMPT：拿不到 `event_deduce` 的六跳事实，
  八项一致性应当不成立（答错、字段缺失或拒答）。
- `collab` 对应协同 Agent 的 COLLAB_PROMPT：拿不到 `collab_run`，就不该出现与离线
  **逐字一致**的 `kernel_fingerprint`，也不该复述出同样的卡片数与处置顺序。

为什么 2026-11-13 补 collab 这一侧：协同 Agent 此前的证据只有"2/2 指纹一致"，
没有对照组——那只能证明"它会照着返回值抄"，证明不了"顺序不是模型自己排的"。
A 组（有工具）与 B 组（无工具）同题对跑，才能把"协同方案来自内核"钉死。

期望结果（两个角色都适用）
--------------------------
对照组复现不出 A 组的凭据。**如果对照组也能复现，说明这套验收测的是模型先验而不是
工具能力，A 组的结论要作废重做。**

用法（宿主机，先 `docker cp` 三个文件进容器）：

    docker cp deploy/nexent/platform_client.py  nexent-config:/tmp/platform_client.py
    docker cp deploy/nexent/setup_nexent.py     nexent-config:/tmp/setup_nexent.py
    docker cp deploy/nexent/no_tools_control.py nexent-config:/tmp/no_tools_control.py
    docker exec nexent-config sh -c "cd /opt/backend && .venv/bin/python /tmp/no_tools_control.py --role collab"

幂等：同名 Agent 已存在时只回写草稿、重新解绑并发布，不会重复创建。
"""

from __future__ import annotations

import argparse
import os

from setup_nexent import (
    COLLAB_CONSTRAINT_PROMPT,
    COLLAB_PROMPT,
    COLLAB_REQUESTED_OUTPUT_TOKENS,
    CONSTRAINT_PROMPT,
    DUTY_PROMPT,
    MAIN_REQUESTED_OUTPUT_TOKENS,
    MODEL_IDS,
    OUR_TOOLS,
    Platform,
)

#: 角色 -> 装配参数。两个角色只在提示词、token 预算与命名上不同，
#: 模型、max_steps、协议修复开关完全一致——否则对照组就多了一个变量。
ROLES = {
    "main": {
        "name": os.getenv("NEXENT_CONTROL_AGENT", "urban_control_no_tools"),
        "display_name": "对照：无 MCP 工具的城市内涝智能体",
        "description": "实验对照组。提示词/模型与主 Agent 完全相同，仅解绑 15 个 MCP 工具，用于证明八项一致来自工具而非模型先验",
        "duty_prompt": DUTY_PROMPT,
        "constraint_prompt": CONSTRAINT_PROMPT,
        "tokens": MAIN_REQUESTED_OUTPUT_TOKENS,
        "version_name": "control-no-tools-v1",
    },
    "collab": {
        "name": os.getenv("NEXENT_COLLAB_CONTROL_AGENT", "urban_collab_control_no_tools"),
        "display_name": "对照：无 MCP 工具的协同总控",
        "description": "实验对照组。提示词/模型与协同 Agent 完全相同，仅解绑协同工具，用于证明动作卡与处置顺序来自 collab_run 而非模型编造",
        "duty_prompt": COLLAB_PROMPT,
        "constraint_prompt": COLLAB_CONSTRAINT_PROMPT,
        "tokens": COLLAB_REQUESTED_OUTPUT_TOKENS,
        "version_name": "control-collab-no-tools-v1",
    },
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="创建零工具对照 Agent（主 Agent / 协同 Agent）")
    parser.add_argument("--role", choices=sorted(ROLES), default="main")
    args = parser.parse_args(argv)
    spec = ROLES[args.role]
    control_name = spec["name"]

    platform = Platform()

    status, agents = platform.call("/api/agent/list")
    agent = None
    for item in agents if isinstance(agents, list) else []:
        if item.get("name") == control_name:
            agent = item
            break

    payload = {
        "name": control_name,
        "display_name": spec["display_name"],
        "description": spec["description"],
        "business_description": "不作为业务入口，仅供验收取证",
        "author": "UrbanEvolve",
        "model_ids": MODEL_IDS,
        "max_steps": 20,
        "duty_prompt": spec["duty_prompt"],
        "constraint_prompt": spec["constraint_prompt"],
        "enable_context_manager": False,
        "enable_protocol_repair_retry": True,
        "requested_output_tokens": spec["tokens"],
    }
    if agent is None:
        status, body = platform.call("/api/agent/update", method="POST", payload=payload)
        print(f"[control:{args.role}] create: {status} {str(body)[:200]}")
        agent = next(
            (a for a in (platform.call("/api/agent/list")[1] or []) if a.get("name") == control_name),
            None,
        )
    else:
        payload["agent_id"] = int(agent["agent_id"])
        status, body = platform.call("/api/agent/update", method="POST", payload=payload)
        print(f"[control:{args.role}] update(draft): {status} {str(body)[:200]}")

    if agent is None:
        print(f"[control:{args.role}] 创建失败")
        return 1

    agent_id = int(agent["agent_id"])
    print(f"[control:{args.role}] id={agent_id} name={control_name}")

    # 零工具：逐个解绑 OUR_TOOLS，按返回码判定——不用 /api/tool/list 验证。
    # 那个接口带 agent_id 也只返回全局工具目录（44 个，与本 Agent 无关），
    # 拿它做"剩余工具"检查会一直报"全都没解绑"，是假验证（2026-11-13 修）。
    tools = platform.tools_by_name()
    failed: list[tuple[str, object]] = []
    for name in sorted(OUR_TOOLS):
        item = tools.get(name)
        if not item:
            failed.append((name, "工具目录里没有"))
            continue
        status, _ = platform.call(
            "/api/tool/update",
            method="POST",
            payload={
                "tool_id": item["tool_id"],
                "agent_id": agent_id,
                "params": {},
                "enabled": False,
                "version_no": 0,
            },
        )
        if status != 200:
            failed.append((name, status))
    if failed:
        print(f"[control:{args.role}] 解绑失败: {failed}")
        print(f"[control:{args.role}] 对照组不成立，停止")
        return 1
    print(f"[control:{args.role}] 已解绑 {len(OUR_TOOLS)}/{len(OUR_TOOLS)} 个 urban 工具")

    status, body = platform.call(
        f"/api/agent/{agent_id}/publish",
        method="POST",
        payload={"version_name": spec["version_name"], "release_note": "零 MCP 工具对照组"},
    )
    print(f"[control:{args.role}] publish: {status} {str(body)[:200]}")

    print(
        f"[control:{args.role}] 如需复核绑定状态（API 查不到，只能查库）：\n"
        f"  docker exec nexent-postgresql psql -U root -d nexent -t -A -c "
        f"\"select count(*) from nexent.ag_tool_instance_t where agent_id={agent_id} and enabled=true;\"\n"
        f"  期望输出 0。另外 A/B 实验中 B 组若复现出指纹，同样说明没解绑干净。"
    )
    print(f"CONTROL_AGENT_ID={agent_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
