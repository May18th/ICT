"""生成初赛「平台能力」证据包的三份 JSON（只读快照，不连平台）。

产物
----
- `samples/nexent/live/agent_config_summary.json`
  三个业务 Agent 的已发布版本、模型、采样参数、执行模式、工具、技能、知识库与记忆状态。
- `samples/nexent/live/mcp_status.json`
  MCP 服务的 15 个工具全量清单 + `/health` 原始返回（全量与 `--groups presentation` 两组）。
- `samples/nexent/live/kb_chunks.json`
  知识库切分口径与实测切片数（直接调 `ingest_knowledge.build_docs()`，与入库同一份代码）。

为什么要生成而不是手写
----------------------
这三份是**证据**，字段一多就一定会漂：平台版本号来自北向快照，工具清单来自源码常量，
导出包又只反映草稿态。手抄一遍等于制造第三种事实。所以每个字段都带 `source`，
读不到的字段明确写 `null` + `unresolved`，不猜。

平台在线/离线都能跑
------------------
平台栈当前没起也能出前两份（读 `agents.json` / `platform_versions.json` / 源码常量）。
只有 `--with-mcp-health` 会在本地临时起一个 MCP SSE 进程抓 `/health`，抓完即杀，
不占端口、不写仓库以外的东西。

用法
----
    PYTHONPATH=src uv run --locked python deploy/nexent/export_capability_pack.py --with-mcp-health
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
LIVE = ROOT / "samples" / "nexent" / "live"
LEDGER = ROOT / "artifacts" / "ledger" / "platform_versions.json"
AGENTS = LIVE / "agents.json"
TOOLS_JSON = ROOT / "samples" / "nexent" / "tools.json"

sys.path.insert(0, str(ROOT / "deploy" / "nexent"))

import setup_nexent  # noqa: E402
from decision_poc.mcp_server import TOOL_GROUPS  # noqa: E402

sys.path.insert(0, str(ROOT / "deploy" / "nexent"))
from ingest_knowledge import build_docs  # noqa: E402

#: 三个业务 Agent 与各自的工具来源。工具数以**已发布版本**为准，导出包只作旁证。
AGENT_PLAN = (
    {
        "name": "urban_deduction_agent",
        "role": "主推演",
        "tools_const": "MAIN_TOOLS",
        "export": ("zip", LIVE / "urban_deduction_agent.zip"),
    },
    {
        "name": "urban_collab_agent",
        "role": "协同总控",
        "tools_const": "COLLAB_TOOLS",
        "export": ("json", LIVE / "urban_collab_agent.export.json"),
    },
    {
        "name": "urban_asset_qa",
        "role": "台账问答",
        "tools_const": None,
        "export": ("json", LIVE / "urban_asset_qa.export.json"),
    },
)


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _export_config(kind: str, path: Path) -> dict:
    """从导出件里取草稿态配置。zip 与 json 两种外壳，字段同名。"""
    if not path.is_file():
        return {"available": False, "path": str(path.relative_to(ROOT))}
    if kind == "zip":
        with zipfile.ZipFile(path) as archive:
            agent = json.loads(archive.read("agent.json").decode("utf-8"))
    else:
        agent = _load(path).get("data") or {}
    info = agent.get("agent_info") or {}
    # 导出结构是 {"agent_info": {"<agent_id>": {...}}}；个别版本直接平铺。
    flat = info.get("name") if "name" in info else next(iter(info.values()), {})
    tools = [t.get("name") or t.get("class_name") for t in (flat.get("tools") or [])]
    return {
        "available": True,
        "path": path.relative_to(ROOT).as_posix(),
        "state": "draft(version_no=0)",
        "tools": tools,
        "tools_count": len(tools),
        "skill_names": flat.get("skill_names") or [],
        "max_steps": flat.get("max_steps"),
        "requested_output_tokens": flat.get("requested_output_tokens"),
        "model_names": flat.get("model_names"),
        "model_params_override": flat.get("model_params_override"),
        "enable_protocol_repair_retry": flat.get("enable_protocol_repair_retry"),
        "context_policy": flat.get("context_policy"),
        "verification_config": flat.get("verification_config"),
        "mcp_info": agent.get("mcp_info") or [],
    }


def _sampling(override: dict | None) -> dict:
    """采样参数：平台侧有没有被覆盖，比给个具体数字更重要。"""
    if not override:
        return {
            "temperature": None,
            "top_p": None,
            "resolution": "未在平台侧覆盖（model_params_override 缺失）→ 取平台/服务商默认",
            "source": "samples/nexent/live/*.export.json 的 agent_info.model_params_override",
        }
    replaced = {
        str(k): v for k, v in override.items()
        if isinstance(v, dict) and any(v.get(key) is not None for key in ("temperature", "top_p"))
    }
    return {
        "temperature": None if not replaced else "见 overrides",
        "top_p": None if not replaced else "见 overrides",
        "overrides": replaced or {},
        "resolution": "未显式设置采样温度（override 只有空 extra_params）→ 取平台/服务商默认",
        "note": "曾试过 temperature=0：模型在工具调用之后返回空回答（conversation 76/77），已回退默认",
        "source": "samples/nexent/live/*.export.json 的 agent_info.model_params_override",
    }


def build_agent_summary() -> dict:
    live = {item["name"]: item for item in _load(AGENTS)["data"]}
    ledger = {item["name"]: item for item in _load(LEDGER)["platform_agents"]}
    tools_contract = {t["name"]: t for t in _load(TOOLS_JSON)["tools"]}

    agents = []
    for plan in AGENT_PLAN:
        name = plan["name"]
        snap, led = live.get(name, {}), ledger.get(name, {})
        exported = _export_config(*plan["export"])
        if plan["tools_const"]:
            tools = sorted(getattr(setup_nexent, plan["tools_const"]))
        else:
            tools = [name for name in exported.get("tools") or [] if name]

        entry = {
            "name": name,
            "display_name": snap.get("display_name"),
            "role": plan["role"],
            "published_version": {
                "current_version_no": snap.get("current_version_no"),
                "version_name": snap.get("version_name") or led.get("version_name"),
                "is_available": snap.get("is_available"),
                "source": "samples/nexent/live/agents.json（北向只读快照）",
                "captured_at": led.get("updated") or _load(LEDGER).get("generated_at"),
            },
            "model": {
                "display_name": snap.get("model_name"),
                "model_names": snap.get("model_names"),
                "api_model": "deepseek-chat",
                "base_url": "https://api.deepseek.com/v1",
                "max_tokens": 8192,
                "source": "deploy/nexent/register_deepseek_model.py（DISPLAY_NAME / MODEL_NAME / MAX_TOKENS）",
            },
            "sampling": _sampling(exported.get("model_params_override")),
            "execution": {
                "mode": "ReAct（工具循环，非规划模式）",
                "enable_planning": False,
                "max_steps": exported.get("max_steps") or snap.get("max_steps"),
                "requested_output_tokens": exported.get("requested_output_tokens"),
                "source": "enable_planning 默认 False 见 Nexent/backend/agents/create_agent_info.py:1318；"
                          "max_steps / requested_output_tokens 取自导出件 agent_info",
                "caveat": "导出件不含 enable_planning 字段，取值来自平台后端默认值；本项目未开启规划模式",
            },
            "tools": tools,
            "tools_source": (
                f"deploy/nexent/setup_nexent.py::{plan['tools_const']}（已发布版本的工具绑定）"
                if plan["tools_const"] else "平台内置 knowledge_base_search（导出件 agent_info.tools）"
            ),
            "tool_descriptions_present": (
                all(t in tools_contract for t in tools) if plan["tools_const"] else None
            ),
            "tool_descriptions_note": (
                None if plan["tools_const"] else "knowledge_base_search 是平台内置工具，不在本项目 tools.json 契约内"
            ),
            "skills": exported.get("skill_names") or [],
            "export_artifact": {
                "available": exported.get("available"),
                "path": exported.get("path"),
                "state": exported.get("state"),
                "tools_in_export": exported.get("tools"),
                "mcp_info": exported.get("mcp_info"),
            },
            "draft_vs_published": {
                "consistent": sorted(exported.get("tools") or []) == tools,
                "note": None,
            },
        }
        if plan["name"] == "urban_asset_qa":
            entry["knowledge_base"] = {
                "bound_tool": "knowledge_base_search",
                "index_name": "urban-flood-assets",
                "embedding_model": "bge-m3（model id 3）",
                "source": "deploy/nexent/ingest_knowledge.py（--index / --embedding-model-id 默认值）",
            }
        if not entry["draft_vs_published"]["consistent"]:
            entry["draft_vs_published"]["note"] = (
                "导出接口固定导草稿（agent_app.py:662 未传 version_no，impl 默认 0），"
                "草稿的工具绑定落后于已发布版本，故导出包不能当该版本的配置证据"
            )
        agents.append(entry)

    return {
        "schema_version": "nexent-agent-config-1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "generator": "deploy/nexent/export_capability_pack.py",
        "sources": {
            "platform_versions": LEDGER.relative_to(ROOT).as_posix(),
            "live_agents": AGENTS.relative_to(ROOT).as_posix(),
            "tools_contract": TOOLS_JSON.relative_to(ROOT).as_posix(),
            "tool_binding": "deploy/nexent/setup_nexent.py",
        },
        "agents": agents,
        "platform_export_defect": {
            "summary": "平台导出只导草稿态，草稿的工具/技能绑定落后于已发布版本；导出包与发布版本不等价",
            "evidence": [
                "Nexent/backend/apps/agent_app.py:662 —— 路由调 export_agent_with_skills_impl 未传 version_no",
                "Nexent/backend/management/services/agent/management.py:1410 —— impl 支持 version_no，默认 0（草稿）",
                "主 Agent 导出包 2 工具、协同 Agent 导出包 0 工具，而发布版本分别为 4 / 6",
            ],
            "workaround": "容器内直调 impl 并传 version_no=<已发布版本号>；命令见 docs/reports/nexent-platform-capability-pack.md §1",
        },
    }


def _http_json(url: str, timeout: float = 5.0) -> dict:
    with urllib.request.urlopen(url, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def capture_mcp_health() -> dict:
    """临时起 MCP SSE，抓全量与 presentation 两组的 /health，抓完杀掉。"""
    port = 8871
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "src") + os.pathsep + env.get("PYTHONPATH", "")
    runs = []
    for groups in (None, "presentation"):
        cmd = [sys.executable, "-m", "decision_poc.mcp_sse", "--host", "127.0.0.1",
               "--port", str(port)]
        if groups:
            cmd += ["--groups", groups]
        proc = subprocess.Popen(cmd, cwd=str(ROOT), env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            payload = None
            for _ in range(40):
                try:
                    payload = _http_json(f"http://127.0.0.1:{port}/health")
                    break
                except (urllib.error.URLError, ConnectionError, TimeoutError):
                    time.sleep(0.25)
            runs.append({
                "command": " ".join(cmd[1:]),
                "groups": groups or "all",
                "health": payload,
            })
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
        port += 1
    return {
        "schema_version": "nexent-mcp-status-1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "method": "本地起 decision_poc.mcp_sse 抓 /health；与平台容器同一份代码（deploy/mcp_sse/Dockerfile COPY src/）",
        "runs": runs,
    }


def build_kb_chunks() -> dict:
    docs = build_docs()
    by_file: dict[str, int] = {}
    for item in docs:
        by_file[item["filename"]] = by_file.get(item["filename"], 0) + 1
    return {
        "schema_version": "nexent-kb-chunks-1",
        "generated_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "method": "deploy/nexent/ingest_knowledge.py::build_docs()（与入库同一份切分代码）",
        "note": "只统计切分结果，不写平台；入库需平台在线（--apply）",
        "total_chunks": len(docs),
        "total_chars": sum(len(item["content"]) for item in docs),
        "file_count": len(by_file),
        "chunks_by_file": dict(sorted(by_file.items())),
        "chunking_policy": {
            "markdown": "按 `## ` 二级标题切；<40 字的空段丢弃；front matter 去掉；每块正文前缀「文档：<文件名>\\n小节：<标题>」",
            "csv": "一行一条记录，正文前缀「台账：<文件名>（列：…）」+「记录 <主键>：k=v；…」；空行跳过",
            "why_not_whole_file": "CSV 整篇当一个块时，8 行台账会被压成一个千字块，命中后模型拿到的仍是全部行，等于没切",
            "locator": {
                "markdown": "urban-assets/<文件>#<节序号>（节序号，不是行号）",
                "csv": "urban-assets/<文件>#row<行号>（含表头，故首条数据为 row2）",
            },
        },
        "sample_chunk": docs[0] if docs else None,
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="生成平台能力证据包 JSON（只读快照）")
    ap.add_argument("--with-mcp-health", action="store_true", help="额外起本地 MCP 抓 /health")
    ap.add_argument("--out-dir", default=str(LIVE))
    args = ap.parse_args(argv)

    out = Path(args.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    payloads = {
        "agent_config_summary.json": build_agent_summary(),
        "kb_chunks.json": build_kb_chunks(),
    }
    if args.with_mcp_health:
        payloads["mcp_status.json"] = capture_mcp_health()

    for filename, payload in payloads.items():
        path = out / filename
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8", newline="\n")
        print(f"已写出 {path.relative_to(ROOT).as_posix()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
