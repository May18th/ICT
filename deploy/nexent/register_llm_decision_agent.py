"""Nexent 平台 LLM 决策对照智能体装配（容器内执行，幂等）。

与 setup_nexent.py 同一通道（platform_client JWT），但**不动既有 Agent**：
- 新建/复用独立 Agent `urban_llm_decision_agent`，绑定 DeepSeek-Chat；
- 不挂任何 MCP 工具（约束行为靠减工具，这是真机 8/8 的经验）；
- 发布后生成北向 API Key 供宿主机侧真机验证。

用法（容器内）：
    /opt/backend/.venv/bin/python /tmp/register_llm_decision_agent.py create
    /opt/backend/.venv/bin/python /tmp/register_llm_decision_agent.py mint-key
    /opt/backend/.venv/bin/python /tmp/register_llm_decision_agent.py status

口径红线：不改动 `urban_deduction_agent`（agent_id=3）的任何字段，其 v10 8/8 验收结论保持原样。
"""
from __future__ import annotations

import json
import sys

from platform_client import PlatformClient, admin_user  # type: ignore

AGENT_NAME = "urban_llm_decision_agent"
DEEPSEEK_MODEL_NAME = "deepseek-chat"

DUTY_PROMPT = (
    "你是城市内涝事件的类型化决策节点。用户输入包含一个决策请求 JSON："
    "request(request_id/goal/context/constraints) 与 evidence 证据列表；"
    "context.options 是唯一候选选项集。你的唯一任务：从 context.options 中选出最优先执行的选项，"
    "并按如下 Schema 输出一个 JSON 对象："
    '{"request_id": str, "summary": str, "options": [{"option_id": str, "description": str}], '
    '"selected_option_id": str, "confidence": float(0-1), "risk_level": "low"|"medium"|"high", '
    '"risks": [str], "evidence": [{"source": str, "version": str|null, "content": str}]}。'
    "options 必须原样沿用 context.options，不得增删改；"
    "evidence 的 source 必须逐字来自输入 evidence 的 source，不得编造来源；"
    "confidence 反映你对该选择的确信程度。"
)

CONSTRAINT_PROMPT = (
    "1. selected_option_id 只能是 context.options 中给出的 id，不得新增选项；\n"
    "2. 若证据不足以支撑处置，必须选择拒答项（description 含「拒答」的选项）；\n"
    "3. request_id 必须原样返回；\n"
    "4. evidence 数组必须至少包含 1 条，每条 source 逐字来自输入 evidence 的 source 字段，"
    "不得为空数组、不得编造来源；\n"
    "5. 只输出一个 JSON 对象，禁止输出 markdown 代码块、解释文字或任何其他内容。"
)


def find_model_id(client: PlatformClient) -> int | None:
    status, body = client.call("/api/model/list")
    if status != 200:
        print(f"[model] list failed: {status} {str(body)[:200]}")
        return None
    for item in body.get("data") or []:
        if item.get("model_name") == DEEPSEEK_MODEL_NAME and item.get("model_type") == "llm":
            print(
                f"[model] model_id={item.get('model_id')} name={item.get('model_name')} "
                f"connect_status={item.get('connect_status')}"
            )
            return int(item["model_id"])
    print(f"[model] 未找到 {DEEPSEEK_MODEL_NAME}，请先运行 register_deepseek_model.py create")
    return None


def find_agent(client: PlatformClient) -> dict | None:
    status, agents = client.call("/api/agent/list")
    if status != 200 or not isinstance(agents, list):
        return None
    for item in agents:
        if item.get("name") == AGENT_NAME:
            return item
    return None


def create_agent(client: PlatformClient, model_id: int) -> int | None:
    agent = find_agent(client)
    payload = {
        "name": AGENT_NAME,
        "display_name": "LLM 决策对照智能体",
        "description": "类型化决策任务的 LLM 对照：在确定性推演给定的选项内选择并解释，与 P8 A/B 同口径",
        "business_description": "城市内涝应急处置的 LLM 决策对照验证",
        "author": "UrbanEvolve",
        "model_ids": [model_id],
        "max_steps": 8,
        "duty_prompt": DUTY_PROMPT,
        "constraint_prompt": CONSTRAINT_PROMPT,
        "enable_context_manager": False,
        "enable_protocol_repair_retry": True,
    }
    if agent is None:
        status, body = client.call("/api/agent/update", method="POST", payload=payload)
        print(f"[agent] create: {status} {str(body)[:200]}")
        agent = find_agent(client)
    else:
        payload["agent_id"] = int(agent["agent_id"])
        status, body = client.call("/api/agent/update", method="POST", payload=payload)
        print(f"[agent] update(draft): {status} {str(body)[:200]}")
        agent = find_agent(client) or agent
    if agent is None:
        print("[agent] 创建失败")
        return None
    agent_id = int(agent["agent_id"])
    print(f"[agent] id={agent_id} name={agent.get('name')} version={agent.get('current_version_no')}")
    status, body = client.call(
        f"/api/agent/{agent_id}/publish",
        method="POST",
        payload={
            "version_name": "llm-decision-v2",
            "release_note": "约束补强：evidence 至少 1 条且 source 逐字引用，修复空证据数组解析失败",
        },
    )
    print(f"[agent] publish: {status} {str(body)[:160]}")
    return agent_id


def mint_api_key(client: PlatformClient) -> str:
    if "/opt/backend" not in sys.path:
        sys.path.insert(0, "/opt/backend")
    from database.token_db import create_token, generate_access_key  # type: ignore

    access_key = generate_access_key()
    info = create_token(access_key=access_key, user_id=admin_user())
    print(f"[key] token_id={info.get('token_id')}")
    print(f"[key] NEXENT_API_KEY={access_key}")
    return access_key


def main(argv: list[str]) -> int:
    client = PlatformClient()
    command = argv[1] if len(argv) > 1 else "status"
    if command == "create":
        model_id = find_model_id(client)
        if model_id is None:
            return 2
        agent_id = create_agent(client, model_id)
        if agent_id is None:
            return 1
        mint_api_key(client)
        return 0
    if command == "mint-key":
        mint_api_key(client)
        return 0
    agent = find_agent(client)
    print(json.dumps(agent, ensure_ascii=False)[:400] if agent else f"agent {AGENT_NAME} 不存在")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
