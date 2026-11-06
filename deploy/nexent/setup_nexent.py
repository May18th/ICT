"""在 Nexent 后端容器内完成城市内涝智能体的平台侧装配（幂等，可重复执行）。

为什么要在容器里跑
------------------
平台管理 API（`/api/...`）用 JWT（HS256，`SUPABASE_JWT_SECRET`）鉴权，JWT 密钥只存在于
`nexent-config` 容器环境；API Key 生成还依赖平台自身的 `database.token_db`。所以这个脚本
设计成"拷进容器、用后端自己的 venv 跑"，而不是在宿主机伪造一套鉴权。

用法（宿主机执行）：

    docker cp deploy/nexent/platform_client.py nexent-config:/tmp/platform_client.py
    docker cp deploy/nexent/setup_nexent.py nexent-config:/tmp/setup_nexent.py
    docker exec nexent-config sh -c "cd /opt/backend && .venv/bin/python /tmp/setup_nexent.py all"

子命令：
    register-mcp   注册 MCP 服务（SSE 地址指向 urban-twin-mcp 容器）
    sync-tools     scan_tool 同步 MCP 工具到平台工具库
    create-agent   创建/复用 Agent，挂 14 个工具，发布版本
    mint-key       生成北向 API Key（打印一次）
    status         打印当前 MCP / Agent / 工具现状
    all            依次执行 register-mcp → sync-tools → create-agent → mint-key
"""

from __future__ import annotations

import json
import os
import sys

from platform_client import PlatformClient, admin_user, config_api

CONFIG_API = config_api()
ADMIN_USER = admin_user()
MCP_NAME = os.getenv("NEXENT_MCP_NAME", "urban-twin-mcp")
MCP_URL = os.getenv("NEXENT_MCP_URL", "http://urban-twin-mcp:8000/sse")
AGENT_NAME = os.getenv("NEXENT_URBAN_AGENT", "urban_deduction_agent")
MODEL_IDS = [int(item) for item in os.getenv("NEXENT_MODEL_IDS", "2").split(",") if item.strip()]

#: 平台在 `requested_output_tokens` 为空时会回落到硬编码 4096（见
#: `nexent/core/models/capacity_resolver.py::_DEFAULT_REQUESTED_OUTPUT_TOKENS`）。
#: 主 Agent 的六跳 JSON 约 3.2k 字符，4096 够用且响应快，显式写出来是为了不让它跟着平台默认值漂；
#: **协同 Agent 的完整动作卡 JSON 超过 4096 token**，会被截断成半截 JSON（表现为"模型输出
#: 格式不稳定"），所以单独放到 8192。模型上限 32768，8192 留足余量。
MAIN_REQUESTED_OUTPUT_TOKENS = int(os.getenv("NEXENT_MAIN_OUTPUT_TOKENS", "4096"))
COLLAB_REQUESTED_OUTPUT_TOKENS = int(os.getenv("NEXENT_COLLAB_OUTPUT_TOKENS", "8192"))

OUR_TOOLS = {
    "semantic_query", "twin_neighbors", "evidence_locate", "claim_trace", "provenance_verify",
    "event_deduce", "counterfactual_run", "conflict_check", "allocation_plan",
    "scene_render", "scene_status", "scene_compare", "asset_lookup",
    "collab_run",
}

#: 主 Agent 只挂"结论类"工具。**故意收窄到 event_deduce + evidence_locate**：
#: openPangu 看到 twin_neighbors/conflict_check/allocation_plan 就会去调（即使提示词明令禁止、
#: 即使 skill 写明"数据已在 event_deduce 返回里"），随后走 parallel_executor 把回答截断在
#: `<|tool_call_start|>`。工具列表才是模型真正读的"提示词"，收窄挂载是唯一稳的约束方式。
#: 协同入口 `collab_run` 挂在专用协同 Agent 上，否则主 Agent 会绕过 event_deduce，
#: 8 项结论一致性校验就守不住。
MAIN_TOOLS = {"event_deduce", "evidence_locate"}

DUTY_PROMPT = """你是城市内涝跨部门决策智能体。用户给出事件编号与时点后，严格按两步执行：

第一步：**第一个动作就必须是调用 MCP 工具 event_deduce**，参数为 {"event_id": "<用户给的事件编号>",
"as_of": "<用户给的时点>", "caller_labels": ["ORG-EMERG"]}。

禁止事项（违反即视为答错）：不要用 python_interpreter 读技能文档或写代码；不要调用 read_skill_md；
不要使用 parallel_executor；不要先输出思考再补工具调用。event_deduce 一次返回就包含六跳全部事实
（observed/affected/owner/conflict/resource/action）、三类冲突、资源缺口与 action_order，
所以拿到返回后**不要再调任何工具**。

第二步：只根据 event_deduce 的返回结果，输出一个 JSON 对象，字段如下（不得增删字段）：
{"event_id": "...", "as_of": "...", "summary": "<一句话结论>",
 "six_hops": ["observed","affected","owner","conflict","resource","action"],
 "conflicts": [{"conflict_id": "C1", "kind": "overlap"}, {"conflict_id": "C2", "kind": "gap"},
               {"conflict_id": "C3", "kind": "contention"}],
 "resource_gap": <整数，从资源缺口读>,
 "action_order": ["<处置动作，按 order 排序>"],
 "residual_risks": ["<二次风险>"],
 "evidence_refs": ["<asset_id#locator>"],
 "handoff_required": <true|false>}

要求：工具返回里没有的数字不要编；JSON 前不要加解释，不要 Markdown 代码块。
"""

CONSTRAINT_PROMPT = (
    "最终回答只输出 JSON 对象本身：不得发起工具调用，不得使用 python_interpreter，"
    "不得使用 parallel_executor，不得输出 Markdown 代码块或解释性文字。"
)

# --- 多智能体协同 Agent（路径 B：总控 Agent 调 collab_run） ----------------
#
# 为什么单独建一个 Agent 而不是改原 Agent 的提示词：
# 原 Agent 的 8 项结论一致性校验（nexent_urban）依赖固定 JSON 结构，
# 把协同输出塞进同一份回答会让"六跳结论"和"部门动作卡"两种产出互相污染。
COLLAB_AGENT_NAME = os.getenv("NEXENT_COLLAB_AGENT", "urban_collab_agent")
COLLAB_TOOLS = {
    "event_deduce", "conflict_check", "allocation_plan", "collab_run",
    "twin_neighbors", "evidence_locate",
}

COLLAB_PROMPT = """你是城市内涝应急协同总控。用户给出事件编号与时点后，严格按两步执行：

第一步：**第一个动作就必须是调用 MCP 工具 collab_run**，参数为 {"event_id": "<用户给的事件编号>",
"as_of": "<用户给的时点>", "caller_labels": ["ORG-EMERG"]}。

禁止事项（违反即视为答错）：不要用 python_interpreter 读技能文档或写代码；不要调用 read_skill_md；
不要使用 parallel_executor。collab_run 一次返回就包含动作卡、签署对、处置顺序与内核指纹，
拿到返回后不要再调用其他工具。

第二步：只根据 collab_run 的返回结果，输出一个 JSON 对象，字段如下（不得增删字段）：
{"event_id": "...", "as_of": "...",
 "covered_agents": ["<已出卡角色>"],
 "cards": [{"card_id": "...", "agent_id": "...", "stage": "...", "action": "...",
           "status": "...", "co_sign_required": ["..."], "depends_on": ["..."]}],
 "co_sign_pairs": [["<部门甲>", "<部门乙>"]],
 "merge_order": ["<card_id，按处置顺序>"],
 "conflicts": [{"conflict_id": "C1", "kind": "overlap"}],
 "constraint_violations": ["<顺序硬约束违规，无则空>"],
 "kernel_fingerprint": "<16 位内核指纹>"}

要求：只转述工具返回，不得新增动作卡或改写冲突；kernel_fingerprint 必须与工具返回逐字一致，
它是"这份协同方案对应哪一次推演"的唯一凭据。JSON 前不要加解释，不要 Markdown 代码块。"""

COLLAB_CONSTRAINT_PROMPT = (
    "最终回答只输出 JSON：不得编造 card_id、动作或指纹；不得使用 python_interpreter 或 parallel_executor；"
    "不要在最终回答里发起工具调用。"
)


class Platform(PlatformClient):
    """平台装配用客户端；JWT 签发与 HTTP 细节统一在 platform_client.py。"""

    # --- MCP ---------------------------------------------------------
    def find_mcp(self):
        status, body = self.call("/api/mcp/list")
        if status != 200 or not isinstance(body, dict):
            return None
        for item in body.get("remote_mcp_server_list", []):
            if item.get("remote_mcp_server_name") == MCP_NAME:
                return item
        return None

    def register_mcp(self) -> int | None:
        record = self.find_mcp()
        if record is None:
            status, body = self.call(
                "/api/mcp/add",
                method="POST",
                payload={
                    "name": MCP_NAME,
                    "server_url": MCP_URL,
                    "description": "城市孪生决策工具：六跳推演/冲突/溯源/分配/渲染（14 个工具）",
                    "source": "local",
                    "enabled": True,
                    "skip_health_check": False,
                },
            )
            print(f"[mcp] add: {status} {str(body)[:200]}")
            record = self.find_mcp()
        if record is None:
            print("[mcp] 注册失败：列表中找不到该服务")
            return None
        print(f"[mcp] id={record.get('mcp_id')} status={record.get('status')} enabled={record.get('enabled')}")
        return int(record["mcp_id"])

    # --- 工具 --------------------------------------------------------
    def tools_by_name(self) -> dict[str, dict]:
        status, body = self.call("/api/tool/list")
        items = body if isinstance(body, list) else (body or {}).get("tools", []) if isinstance(body, dict) else []
        return {item.get("name"): item for item in items if isinstance(item, dict)}

    def sync_tools(self) -> dict[str, dict]:
        status, _ = self.call("/api/tool/scan_tool")
        print(f"[tools] scan_tool: {status}")
        tools = self.tools_by_name()
        missing = sorted(OUR_TOOLS - set(tools))
        print(f"[tools] 命中 {len(OUR_TOOLS) - len(missing)}/{len(OUR_TOOLS)}；缺失 {missing}")
        return tools

    # --- Agent -------------------------------------------------------
    def find_agent(self):
        status, agents = self.call("/api/agent/list")
        if status != 200 or not isinstance(agents, list):
            return None
        for item in agents:
            if item.get("name") == AGENT_NAME:
                return item
        return None

    def create_agent(self) -> int | None:
        agent = self.find_agent()
        payload = {
            "name": AGENT_NAME,
            "display_name": "城市内涝跨部门决策智能体",
            "description": "基于 Nexent + MCP 的城市内涝跨部门推演：六跳结论、三类冲突、稀缺资源分配与证据链",
            "business_description": "面向城市运行管理中心的应急决策辅助",
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
            status, body = self.call("/api/agent/update", method="POST", payload=payload)
            print(f"[agent] create: {status} {str(body)[:200]}")
            agent = self.find_agent()
        else:
            # 复用已有 Agent 时也必须回写草稿：否则改了 DUTY_PROMPT 却发布旧提示词，
            # 表现为"脚本改了、平台行为没变"，只能靠翻渠道日志才发现。
            payload["agent_id"] = int(agent["agent_id"])
            status, body = self.call("/api/agent/update", method="POST", payload=payload)
            print(f"[agent] update(draft): {status} {str(body)[:200]}")
            agent = self.find_agent() or agent
        if agent is None:
            print("[agent] 创建失败")
            return None
        agent_id = int(agent["agent_id"])
        print(f"[agent] id={agent_id} name={agent.get('name')} version={agent.get('current_version_no')}")
        return agent_id

    def find_collab_agent(self):
        status, agents = self.call("/api/agent/list")
        if status != 200 or not isinstance(agents, list):
            return None
        for item in agents:
            if item.get("name") == COLLAB_AGENT_NAME:
                return item
        return None

    def create_collab_agent(self) -> int | None:
        agent = self.find_collab_agent()
        payload = {
            "name": COLLAB_AGENT_NAME,
            "display_name": "城市内涝应急协同总控",
            "description": "把六跳推演拆成 1 总控 + 5 专业部门的动作卡，编排联合发布与处置顺序",
            "business_description": "面向城市运行管理中心的跨部门任务分派",
            "author": "UrbanEvolve",
            "model_ids": MODEL_IDS,
            "max_steps": 20,
            "duty_prompt": COLLAB_PROMPT,
            "constraint_prompt": COLLAB_CONSTRAINT_PROMPT,
            "enable_context_manager": False,
            "enable_protocol_repair_retry": True,
            "requested_output_tokens": COLLAB_REQUESTED_OUTPUT_TOKENS,
        }
        if agent is None:
            status, body = self.call("/api/agent/update", method="POST", payload=payload)
            print(f"[collab] create: {status} {str(body)[:200]}")
            agent = self.find_collab_agent()
        else:
            payload["agent_id"] = int(agent["agent_id"])
            status, body = self.call("/api/agent/update", method="POST", payload=payload)
            print(f"[collab] update(draft): {status} {str(body)[:200]}")
            agent = self.find_collab_agent() or agent
        if agent is None:
            print("[collab] 创建失败")
            return None
        agent_id = int(agent["agent_id"])
        print(f"[collab] id={agent_id} name={agent.get('name')} version={agent.get('current_version_no')}")
        return agent_id

    def attach_tools_for(self, agent_id: int, names: set[str]) -> None:
        tools = self.tools_by_name()
        for name in sorted(names):
            item = tools.get(name)
            if not item:
                print(f"[tools] 缺少 {name}，跳过挂载")
                continue
            status, _ = self.call(
                "/api/tool/update",
                method="POST",
                payload={"tool_id": item["tool_id"], "agent_id": agent_id, "params": {}, "enabled": True, "version_no": 0},
            )
            print(f"[tools] attach {name}({item['tool_id']}) -> {agent_id}: {status}")
        # 反向清理：不属于本 Agent 的工具显式禁用，避免历史挂载残留
        for name in sorted(OUR_TOOLS - names):
            item = tools.get(name)
            if not item:
                continue
            status, _ = self.call(
                "/api/tool/update",
                method="POST",
                payload={"tool_id": item["tool_id"], "agent_id": agent_id, "params": {}, "enabled": False, "version_no": 0},
            )
            print(f"[tools] detach {name}({item['tool_id']}) from {agent_id}: {status}")

    def attach_collab_tools(self, agent_id: int) -> None:
        self.attach_tools_for(agent_id, COLLAB_TOOLS)

    def publish_collab(self, agent_id: int) -> None:
        status, body = self.call(
            f"/api/agent/{agent_id}/publish",
            method="POST",
            payload={"version_name": "urban-collab-v1", "release_note": "两步式提示词：先 collab_run 再转述动作卡"},
        )
        print(f"[collab] publish: {status} {str(body)[:200]}")

    def attach_tools(self, agent_id: int) -> None:
        self.attach_tools_for(agent_id, MAIN_TOOLS)

    def publish(self, agent_id: int) -> None:
        status, body = self.call(
            f"/api/agent/{agent_id}/publish",
            method="POST",
            payload={"version_name": "urban-mcp-v3", "release_note": "两步式提示词：先 event_deduce 再输出 JSON"},
        )
        print(f"[agent] publish: {status} {str(body)[:200]}")

    # --- API Key -----------------------------------------------------
    def mint_api_key(self) -> str:
        # 平台模块挂在 /opt/backend 下；从 /tmp 执行脚本时不在 sys.path，需显式补
        if "/opt/backend" not in sys.path:
            sys.path.insert(0, "/opt/backend")
        from database.token_db import create_token, generate_access_key  # type: ignore

        access_key = generate_access_key()
        info = create_token(access_key=access_key, user_id=ADMIN_USER)
        print(f"[key] token_id={info.get('token_id')}")
        print(f"[key] NEXENT_API_KEY={access_key}")
        return access_key


def main(argv: list[str]) -> int:
    command = argv[1] if len(argv) > 1 else "status"

    # 帮助/非法命令不应先初始化平台客户端；宿主机通常没有容器内的 PyJWT
    # 与 SUPABASE_JWT_SECRET，但仍应能查看用法和发现拼写错误。
    if command in {"-h", "--help", "help"}:
        print(__doc__)
        return 0
    valid_commands = {"all", "register-mcp", "sync-tools", "create-agent", "create-collab-agent", "mint-key", "status"}
    if command not in valid_commands:
        print(__doc__)
        return 2

    platform = Platform()

    if command in {"all", "register-mcp", "sync-tools", "create-agent", "create-collab-agent"}:
        mcp_id = platform.register_mcp()
        if mcp_id is None:
            return 1
        if command in {"all", "sync-tools", "create-agent", "create-collab-agent"}:
            platform.sync_tools()
        if command in {"all", "create-agent"}:
            agent_id = platform.create_agent()
            if agent_id is None:
                return 1
            platform.attach_tools(agent_id)
            platform.publish(agent_id)
        if command in {"all", "create-collab-agent"}:
            collab_id = platform.create_collab_agent()
            if collab_id is None:
                return 1
            platform.attach_collab_tools(collab_id)
            platform.publish_collab(collab_id)
        if command == "all":
            platform.mint_api_key()
        return 0

    if command == "mint-key":
        platform.mint_api_key()
        return 0

    if command == "status":
        record = platform.find_mcp()
        print("[mcp]", json.dumps(record, ensure_ascii=False)[:300] if record else "未注册")
        tools = platform.tools_by_name()
        print("[tools] 命中:", sorted(OUR_TOOLS & set(tools)))
        agent = platform.find_agent()
        print("[agent]", json.dumps(agent, ensure_ascii=False)[:400] if agent else "未创建")
        collab = platform.find_collab_agent()
        print("[collab-agent]", json.dumps(collab, ensure_ascii=False)[:400] if collab else "未创建")
        return 0

    raise AssertionError(f"unreachable command: {command}")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
