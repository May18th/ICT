#!/usr/bin/env python3
"""创建「内涝资产台账问答」Agent，把平台知识库接到可演示的入口上。

为什么不把 knowledge_base_search 挂到主 Agent（urban_deduction_agent）：
    主 Agent 的两步式设计是"MCP 工具 event_deduce 一次拿全量事实，之后不再调工具"。
    多挂一个检索工具，模型会绕过推演直接检索台账，六跳结论的确定性就没了
    （这个坑在 setup_nexent.py 的 MAIN_TOOLS 注释里已经记过一次）。

    所以知识库单独开一个职责单一的 Agent：只回答"台账里写了什么"，
    不做推演、不给处置建议。演示时两条线互不干扰。

与 doc_search 的分工：
    doc_search  = 自建资产台账检索（MCP，返回结构化条目）
    knowledge_base_search = 平台知识库（ES 向量 + 混合检索，返回切片与出处）
    两者都指向同一批源文件，但入口不同：前者给推演链路取证，后者给人提问。

用法：
    python deploy/nexent/create_asset_qa_agent.py --dry-run
    python deploy/nexent/create_asset_qa_agent.py --apply \
        --index-id 3-4829593e644e4ef298ddcaba37c4df37

环境变量同 setup_nexent.py：NEXENT_WEB_BASE / NEXENT_ADMIN_EMAIL / NEXENT_SUPER_ADMIN_PASSWORD
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

BASE = os.getenv("NEXENT_WEB_BASE", "http://127.0.0.1:3000")
EMAIL = os.getenv("NEXENT_ADMIN_EMAIL", "suadmin@nexent.com")
AGENT_NAME = os.getenv("NEXENT_ASSET_QA_AGENT", "urban_asset_qa")
MODEL_IDS = [int(x) for x in os.getenv("NEXENT_MODEL_IDS", "5").split(",")]

DUTY_PROMPT = """你是城市内涝资产台账问答助手。你的唯一职责是：**依据平台知识库里的台账与预案原文，回答"文件里写了什么"**。

执行方式（必须遵守）：
1. 收到问题后，**第一步必须调用 knowledge_base_search**，用问题里的设施编号/路段/部门名作为检索词；
   top_k 用默认值即可，不要自己编造 index 名。
2. 只根据检索返回的切片作答。切片里没有的信息，直接说"台账中未记录"，**不要推测、不要补全**。
3. 每条结论后面用 `（出处：<path_or_url>）` 标注来源，一条结论一个出处。
4. 涉及数值（容量、深度、阈值、数量）时，原样引用台账里的数字和单位，不要换算。

禁止事项：
- 不要给处置建议或推演结论——那是另一个智能体（城市内涝跨部门决策智能体）的职责；
- 不要调用 event_deduce / allocation_plan / collab_run 等推演工具；
- 不要使用 python_interpreter 读文件；
- 检索不到就说检索不到，**不要用常识填补台账空白**。

回答格式：
先一句结论，再列 2~5 条要点，每条带出处。总长度控制在 300 字以内。
"""

GREETING = """我是内涝资产台账问答助手。

可以问我这几类问题：
· 某台设施现在什么状态、容量多少、谁负责维护
· 某个路段的等级、车道数、所属片区、是否易涝
· 某个部门的职责范围与上报路径
· 预案条款里对某个场景是怎么规定的

我只回答"台账和预案原文里写了什么"，每条结论都会标出处。
处置建议请找「城市内涝跨部门决策智能体」。"""

EXAMPLE_QUESTIONS = [
    "DF-PS-01 泵站现在是什么状态？设计流量多少？运维主体是谁？",
    "兴华路下穿隧道的封控阈值是多少？有没有独立泵房？",
    "移动抽水泵一共有几台？存放在哪个养护站？单台流量多大？",
    "哪个部门负责雨水口清掏？泵站运维主体在文件里界定清楚了吗？",
]


def http(path: str, data=None, method=None, jwt: str | None = None, timeout=120):
    headers = {"Content-Type": "application/json"}
    if jwt:
        headers["Authorization"] = "Bearer " + jwt
    body = json.dumps(data, ensure_ascii=False).encode() if data is not None else None
    req = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()[:300]


def signin() -> str:
    pw = os.getenv("NEXENT_SUPER_ADMIN_PASSWORD", "").strip()
    if not pw:
        raise SystemExit("缺少 NEXENT_SUPER_ADMIN_PASSWORD")
    req = urllib.request.Request(
        BASE + "/api/user/signin",
        data=json.dumps({"email": EMAIL, "password": pw}).encode(),
        headers={"Content-Type": "application/json"},
    )
    for c in urllib.request.urlopen(req, timeout=30).headers.get_all("Set-Cookie") or []:
        m = re.match(r"nexent_access_token=([^;]+)", c)
        if m:
            return m.group(1)
    raise SystemExit("登录未下发 cookie")


def find_agent(jwt: str) -> dict | None:
    """按 name 找。注意 /api/agent/list 返回的是"每个 Agent 一行"（current_version_no），
    不是草稿行；早先按 version_no==0 过滤导致"创建成功却查不到"（踩过一次）。"""
    st, body = http("/api/agent/list", None, "GET", jwt)
    if st != 200:
        return None
    for a in json.loads(body):
        if a.get("name") == AGENT_NAME:
            return a
    return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--index-id", required=False, default="",
                    help="知识库真实索引 id（形如 3-4829...），多个用逗号分隔")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    payload = {
        "name": AGENT_NAME,
        "display_name": "内涝资产台账问答",
        "description": "基于平台知识库回答设施状态、路段属性、部门职责与预案条款；每条结论标出处",
        "business_description": "面向值班人员的台账速查，避免翻 CSV 找字段",
        "author": "UrbanEvolve",
        "model_ids": MODEL_IDS,
        "max_steps": 8,
        "duty_prompt": DUTY_PROMPT,
        "greeting_message": GREETING,
        "example_questions": EXAMPLE_QUESTIONS,
        "enable_context_manager": False,
        "enable_protocol_repair_retry": True,
    }

    print(f"Agent: {AGENT_NAME} / 内涝资产台账问答")
    print(f"示例问题 {len(EXAMPLE_QUESTIONS)} 条：")
    for q in EXAMPLE_QUESTIONS:
        print(f"  - {q}")
    print(f"知识库索引：{args.index_id or '（未指定，需 --index-id）'}")

    if args.dry_run or not args.apply:
        print("\n[dry-run] 未写入。")
        return 0
    if not args.index_id:
        raise SystemExit("--apply 需要 --index-id")
    index_ids = [s.strip() for s in args.index_id.split(",") if s.strip()]

    jwt = signin()
    existing = find_agent(jwt)
    if existing:
        payload["agent_id"] = int(existing["agent_id"])
        print(f"\n复用已有 Agent id={payload['agent_id']}，回写草稿")
    st, body = http("/api/agent/update", payload, "POST", jwt)
    print(f"[agent] {'update' if existing else 'create'}: {st} {body[:160]}")
    if st != 200:
        return 1

    agent = find_agent(jwt)
    if not agent:
        print("创建后查不到该 Agent")
        return 1
    agent_id = int(agent["agent_id"])

    # 只挂 knowledge_base_search；index_names 必须是真实索引 id
    # 注意 /api/tool/list 返回的是**数组**（43 个工具），早先写成 startswith("{") 判断
    # 导致解析成空列表、报"工具库里找不到"（踩过一次）。
    st, tools_body = http("/api/tool/list", None, "GET", jwt)
    try:
        parsed = json.loads(tools_body)
    except Exception:
        parsed = []
    tools = parsed if isinstance(parsed, list) else (parsed.get("tools") or parsed.get("data") or [])
    item = next((t for t in tools if t.get("name") == "knowledge_base_search"), None)
    if not item:
        print("工具库里找不到 knowledge_base_search")
        return 1
    st, body = http("/api/tool/update", {
        "tool_id": item["tool_id"], "agent_id": agent_id, "version_no": 0, "enabled": True,
        "params": {
            "top_k": 4,
            "index_names": index_ids,
            "search_mode": "hybrid",
            "rerank": False,
            "rerank_model_name": "",
        },
    }, "POST", jwt)
    print(f"[tools] knowledge_base_search -> {agent_id}: {st} {body[:120]}")

    st, body = http(f"/api/agent/{agent_id}/publish", {
        "version_name": "urban-asset-qa-v1",
        "release_note": "台账问答：knowledge_base_search 检索平台知识库，每条结论标出处",
    }, "POST", jwt)
    print(f"[agent] publish: {st} {body[:160]}")
    print(f"\n完成。agent_id={agent_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
