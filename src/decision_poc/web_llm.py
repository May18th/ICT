"""Web demo 的可选 LLM 决策端点支撑。

口径与 P8 A/B 评测（``llm_ab.py``）**完全同源**：

- 复用 ``urban_eval`` 用例 N01（核心事件六跳齐全）与 ``llm_ab.build_task``：
  选项由确定性推演生成、参照项 = 推演第一处置动作、证据池从推演报告抽取；
- 模型调用走 ``generate_decision_live``（与 ``llm_ab._run_task`` 的 deepseek
  分支同一条路径），temperature 默认钉 0；
- 未配置 ``DECISION_LLM_PROVIDER=deepseek`` / ``DEEPSEEK_API_KEY`` 时返回
  ``configured=false``，页面展示"未配置"，不影响其余演示；
- 本模块不回显 API key；所有异常只暴露类型与消息。
"""
from __future__ import annotations

import asyncio
import os
import time
from typing import Any

PROVIDER_ENV = "DECISION_LLM_PROVIDER"
API_KEY_ENV = "DEEPSEEK_API_KEY"
DEMO_CASE_ID = "N01"  # 核心事件六跳齐全：与首页推演同一 event/as_of
LLM_TIMEOUT_SECONDS = 45.0


def _unconfigured(reason: str) -> dict[str, Any]:
    return {
        "configured": False,
        "reason": reason,
        "hint": f"设置 {PROVIDER_ENV}=deepseek 与 {API_KEY_ENV} 后重启服务即可启用",
    }


def llm_decision_payload() -> dict[str, Any]:
    """对 A/B 用例 N01 跑一次真实 LLM 决策并对照确定性参照项；未配置时优雅降级。"""

    provider = (os.environ.get(PROVIDER_ENV) or "").strip().lower()
    api_key = (os.environ.get(API_KEY_ENV) or "").strip()
    if provider != "deepseek":
        return _unconfigured(f"{PROVIDER_ENV} 未设置为 deepseek（当前: {provider or '未设置'}）")
    if not api_key:
        return _unconfigured(f"{API_KEY_ENV} 未设置")

    # 延迟导入：保持 web_demo 主链路不硬依赖 pydantic_ai。
    try:
        from .agent import generate_decision_live
        from .llm_ab import build_task
        from .model_provider import ModelProviderError, build_model, settings_from_env
        from .skills import SkillPipeline, SkillRuntime
        from .urban_eval import load_cases
    except ImportError as exc:
        return _unconfigured(f"服务端缺少可选依赖: {type(exc).__name__}: {exc}")

    try:
        settings = settings_from_env()
        build_model(settings)
    except ModelProviderError as exc:
        return _unconfigured(str(exc))

    case = next((c for c in load_cases() if c.case_id == DEMO_CASE_ID), None)
    if case is None:
        return {"configured": True, "status": "error", "error": f"找不到用例 {DEMO_CASE_ID}"}

    try:
        report = SkillPipeline(SkillRuntime()).run(
            case.event_id, case.as_of, caller_labels=case.caller_labels, persist=False
        )
    except Exception as exc:  # noqa: BLE001 - 推演失败按错误返回，不暴露内部栈
        return {
            "configured": True,
            "status": "error",
            "error": f"确定性推演失败: {type(exc).__name__}: {exc}",
        }
    task = build_task(case, report)

    started = time.perf_counter()
    try:
        decision = asyncio.run(
            asyncio.wait_for(
                generate_decision_live(task.request, task.evidence, settings),
                timeout=LLM_TIMEOUT_SECONDS,
            )
        )
    except asyncio.TimeoutError:
        return {
            "configured": True,
            "status": "error",
            "error": f"LLM 调用超过 {LLM_TIMEOUT_SECONDS:g}s 超时",
            "model": settings.model_name,
        }
    except ModelProviderError as exc:
        return {
            "configured": True,
            "status": "error",
            "error": str(exc),
            "model": settings.model_name,
        }
    except Exception as exc:  # noqa: BLE001 - 网络层/SDK 异常统一暴露类型与消息
        return {
            "configured": True,
            "status": "error",
            "error": f"{type(exc).__name__}: {exc}",
            "model": settings.model_name,
        }
    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)

    selected = decision.selected_option_id
    return {
        "configured": True,
        "status": "ok",
        "case_id": case.case_id,
        "event_id": case.event_id,
        "as_of": case.as_of,
        "model": settings.model_name,
        "temperature": settings.temperature,
        "latency_ms": elapsed_ms,
        "request_id": decision.request_id,
        "selected_option_id": selected,
        "selected_description": next(
            (opt["description"] for opt in task.options if opt["option_id"] == selected), ""
        ),
        "confidence": decision.confidence,
        "risk_level": str(decision.risk_level),
        "summary": decision.summary,
        "risks": decision.risks,
        "options": task.options,
        "expected_option_id": task.expected_option_id,
        "selected_match": selected == task.expected_option_id,
        "evidence_cited": len(decision.evidence),
    }


__all__ = ["llm_decision_payload"]
