"""One-command smoke check for a live Nexent deployment.

There is nothing to configure until a Nexent instance exists. Once it does:

    $env:NEXENT_BASE_URL   = "http://<host>:5013"
    $env:NEXENT_API_KEY    = "<Access Key generated when publishing the agent>"
    $env:NEXENT_AGENT_NAME = "<published agent name>"
    $env:PYTHONPATH = "src"
    uv run --locked python -m decision_poc.nexent_smoke

Exit codes: 0 ok, 1 Nexent call failed, 2 credentials missing.
"""

import json
import os
import sys
from typing import Any

from .contracts import DecisionRequest
from .nexent_adapter import NexentAdapter
from .nexent_transport import (
    NexentConfigError,
    NexentError,
    NexentHttpTransport,
    NexentSettings,
)

REQUIRED_ENV = ("NEXENT_API_KEY", "NEXENT_AGENT_NAME")


def missing_env(environ: dict[str, str] | None = None) -> list[str]:
    """Return the required environment variables that are absent or blank."""
    source = os.environ if environ is None else environ
    return [name for name in REQUIRED_ENV if not str(source.get(name, "")).strip()]


def run(
    *,
    settings: NexentSettings | None = None,
    transport: Any | None = None,
    out=sys.stdout,
) -> int:
    missing = [] if settings is not None else missing_env()
    if missing:
        out.write(
            "缺少环境变量：" + ", ".join(missing) + "\n"
            "这三个值都来自你自己的 Nexent 部署，无法自动生成：\n"
            "  NEXENT_BASE_URL   部署地址，默认 http://localhost:5013\n"
            "  NEXENT_API_KEY    发布 Agent 后生成的 Access Key\n"
            "  NEXENT_AGENT_NAME 已发布 Agent 的名称\n"
        )
        return 2

    if settings is None:
        try:
            resolved = NexentSettings.from_env()
        except NexentConfigError as exc:
            out.write(f"配置无效：{exc}\n")
            return 2
    else:
        resolved = settings

    invoke = transport or NexentHttpTransport(settings=resolved)
    try:
        result = NexentAdapter(invoke=invoke).run(
            DecisionRequest(
                request_id="req-smoke",
                goal=(
                    "设备A-101振动值由2.1mm/s升至7.8mm/s、轴承温度85°C，"
                    "产线L2不能停，如何在4小时内处置"
                ),
                context={
                    "device": "A-101",
                    "vibration_mm_s": 7.8,
                    "temp_c": 85,
                    "line": "L2",
                },
                constraints=["不停产", "4小时内闭环", "给出现场可执行步骤"],
            )
        )
    except NexentError as exc:
        out.write(f"调用失败：{type(exc).__name__}: {exc}\n")
        return 1

    preview = dict(result)
    answer = preview.get("answer")
    if isinstance(answer, str):
        preview["answer_length"] = len(answer)
        preview["answer"] = answer[:300] + ("..." if len(answer) > 300 else "")
    out.write(json.dumps(preview, ensure_ascii=False, indent=2) + "\n")
    if result.get("decision") is None:
        out.write(
            "提示：链路已通，但 Agent 未返回可解析的 DecisionOutput JSON；"
            f"decision_error={result.get('decision_error')}\n"
        )
    return 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
