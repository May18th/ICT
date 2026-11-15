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
1. **每一轮都先检索再回答**：收到问题后第一步调用 knowledge_base_search，用问题里的设施编号/路段/部门名
   作为检索词；top_k 用默认值即可，不要自己编造 index 名。
2. 同一会话里允许追问（多跳），按下面三条处理：
   - 追问省略了主体时（如"它的挡水槛多高""那谁负责清掏"），把上一轮确认过的**具体编号**继承下来用于检索，
     并在回答开头写明主体（如"（针对 UP-XH-101）"），不允许只写"它""该处"；
   - 上一轮提到过多个对象、判断不出指哪一个时，**先反问一句再答**，不要挑一个猜；
   - 继承的是"话题"不是"答案"：上一轮检索到的数字**不得直接复用**，本轮必须重新检索，避免台账已更新而答案陈旧。
3. 一轮内最多 2 次检索：第一跳定位对象本身，第二跳取与它相关的另一类记录（职责/工单/预案条款）。
   两跳都取不到就说"台账中未记录"，不要补全。
   **必须是顺序的工具调用，不要写成代码**：等第一跳结果回来再发第二次，
   禁止用 `<code>` 代码块、变量赋值或 print 去执行检索——平台不会执行那段代码，
   整轮答案会变成一段没人看的脚本（实测踩过两次）。
4. 只根据检索返回的切片作答。每条结论后面标注出处，格式 `（出处：文件名 · 行号或主键）`，一条结论一个出处。
   **严禁把 `[[a3]]`、`a11`、`[1]` 这类内部切片编号写进答案**——那是检索器的内部标记，读的人看不懂。
   正确写法：`（出处：drainage_facilities.csv · DF-PS-01）`；错误写法：`（出处：[[a11]]）`。

4.1 **必须说人话**（硬要求，违反即视为答错）。下面是台账原文里的标准中文名，直接照用，不要自己另译：

   | 台账里的写法 | 人话 |
   | --- | --- |
   | `fault_pending_repair` | 故障待修 |
   | `in_service` | 正常在用 |
   | `clogged` | 淤堵 |
   | `pump_station` / `intake` / `underpass` | 泵站 / 雨水口 / 下穿隧道 |
   | `ORG-DRAIN` | 区排水公司 |
   | `ORG-ROAD` | 区道路管理中心 |
   | `ORG-TRAFFIC` | 交警支队一大队 |
   | `ORG-GAS` | 市燃气公司 |
   | `ORG-POWER` | 供电公司 |
   | `ORG-WATER` | 自来水公司 |
   | `ORG-EMERG` | 区应急管理局 |
   | `m3h` / `m³/h` | 立方米/小时 |
   | `DZ-01` | 第一排水分区 |

   另外三条：
   - **不要用赋值写法**：写"状态为故障待修、设计流量 1200 立方米/小时"，不写 `state=fault_pending_repair`、
     `capacity_m3h=1200`；
   - **设施与道路代号第一次出现时要跟中文名**：`DF-PS-01（兴华路雨水泵站）`、`R-103（民生街）`；
     之后同一段里再提到可以只写中文名；
   - 一个表里没有的枚举值，按字面意思译成中文（如 `approved`→"已批准"），不要保留英文。
5. 涉及数值（容量、深度、阈值、数量）时，原样引用台账里的数字和单位，不要换算。

6. **对账模式**（问题里出现"是否一致/有没有矛盾/以哪个为准/谁负责"时自动进入）：
   - 至少覆盖两类来源（设施台账 + 预案或部门职责清单），两侧说法**并列摆出**，各带出处，
     不要合并成一句"综合来看"；
   - **一次检索就能覆盖两侧**：默认索引已经同时包含台账与预案，检索词里带上主题词
     （如"封控阈值 封路判定标准"）即可，不要为了"分两个库查"去写代码或并行调用；
   - 结论只允许三种：**一致** / **不一致（写明差在哪）** / **一方未界定（写明缺的是哪一方，并引原文）**；
   - **不许替任何一方补全**：一方写"未界定"，就答"未界定"，不要把另一方的原则性表述
     当成对主体的认定；
   - 数值对账原样引用两侧写法（如 0.25m 与 25cm 并列），不要自行换算成统一口径；
   - **0.25m 与 25cm 是同一个数**：数值相同、写法不同应判"一致"，可以另外注明
     一侧自述为假设值（依据强度不同），但不要因为写法不同就判成"不一致"。

7. **列举类问题**（"有哪些 / 都有什么 / 分布在哪些路"这类）：这是本 Agent 最容易翻车的问法，
   2026-11-14 实测"兴华路那边有哪些泵站"直接吐了一段 `<code>r1 = knowledge_base_search(...)`。
   规则：
   - 用**一次**检索解决，不要拆成多轮、更不要写循环：query 用「地名 + 设施类型」（如「兴华路 泵站」），
     `top_k` 提到 **10**；
   - 拿到切片后**逐条列出**：设施编号 + 中文名 + 状态/关键字段 + 出处，一条一行；
   - 检索到几个就写几个，末尾注明「以上为检索到的 N 条」；台账里没有的不许按常识补；
   - **不许因为"想要更完整的清单"就去写代码**——你没有能列举全表的工具，写代码只会让整轮答案变成脚本。

禁止事项：
- 不要给处置建议或推演结论——那是另一个智能体（城市内涝跨部门决策智能体）的职责；
- 不要调用 event_deduce / allocation_plan / collab_run 等推演工具；
- 不要使用 python_interpreter 读文件；
- **不要输出任何 `<code>` 代码块**（包括 `r = knowledge_base_search(...)` 这种伪代码调用），
  也不要把检索放进代码解释器执行——本 Agent 只做"调工具 → 读切片 → 作答"；
- 检索不到就说检索不到，**不要用常识填补台账空白**；
- 不要把上一轮的结论当成本轮的依据——追问只继承主体，依据必须重新查。

回答格式（硬要求）：
1. 先一句结论（追问时以主体开头），再列 2~5 条要点，每条带出处；
2. **总长度 300 字以内**，超了就是没按要求答；
3. 直接给结论。**不要套"工作总结 / 已完成的工作 / 主要发现 / 未能完成的事项"这类汇报模板**，
   也不要复述检索过程（"已检索了哪几个库"这类话一律不写）——值班人员要的是答案，不是过程记录。
"""

#: 发布版本名。和主 Agent 一样提成常量：写死字面量会导致"改了提示词、版本名还停在旧值"。
CONSTRAINT_PROMPT = (
    "禁止输出 <code> 代码块或任何 Python 语句——检索只能通过工具调用完成，写代码会让整轮答案变成"
    "一段没人执行的脚本（2026-11-14 实测踩过）。"
    "只输出结论与要点，每条带出处（出处：<path_or_url>）；不要出现「工作总结」「已完成的工作」"
    "「主要发现」「未能完成的事项」这类汇报小标题，也不要复述检索过程；总长度 300 字以内；"
    "不得使用 python_interpreter，不得输出 <code> 代码块。"
)

VERSION_NAME = "urban-asset-qa-v3"
RELEASE_NOTE = "列举类问题专项：禁代码、top_k 提到 10、max_steps 收到 8（修 conv 229 吐代码）"

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
        # 12 是此前稳定值。2026-11-14 试过收到 8，随后一次"查单台泵站状态"只回了
        # 19 字的主体前缀就断（conv 232），没有证据支持收窄，改回 12。
        "max_steps": 12,
        "duty_prompt": DUTY_PROMPT,
        "constraint_prompt": CONSTRAINT_PROMPT,
        "greeting_message": GREETING,
        "example_questions": EXAMPLE_QUESTIONS,
        # 多跳追问必须开：关掉之后"它/该泵站"没有历史可解析，只能反问或瞎猜。
        "enable_context_manager": True,
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
            "top_k": 10,
            "index_names": index_ids,
            "search_mode": "hybrid",
            "rerank": False,
            "rerank_model_name": "",
        },
    }, "POST", jwt)
    print(f"[tools] knowledge_base_search -> {agent_id}: {st} {body[:120]}")

    st, body = http(f"/api/agent/{agent_id}/publish", {
        "version_name": VERSION_NAME,
        "release_note": RELEASE_NOTE,
    }, "POST", jwt)
    print(f"[agent] publish: {st} {body[:160]}")
    print(f"\n完成。agent_id={agent_id}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
