"""Smoke test for the optional real-model provider (DeepSeek).

Offline by default:

    uv run --group llm python scripts/deepseek_smoke.py          # prints skip if unconfigured
    $env:DECISION_LLM_PROVIDER = "deepseek"
    $env:DEEPSEEK_API_KEY      = "<your key>"
    uv run --group llm python scripts/deepseek_smoke.py

The script never echoes the API key. It only persists the structured
``DecisionOutput`` that the typed node returns.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

# Windows 控制台默认不是 UTF-8，中文输出会乱码；只影响展示，不影响数据
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from decision_poc.agent import generate_decision_live  # noqa: E402
from decision_poc.contracts import DecisionRequest  # noqa: E402
from decision_poc.model_provider import (  # noqa: E402
    API_KEY_ENV,
    PROVIDER_ENV,
    ModelProviderError,
    settings_from_env,
)

REQUEST = DecisionRequest(
    request_id="req-deepseek-smoke",
    goal="为暴雨内涝事件选择先期处置方案",
    context={"event_id": "WO-20261029-007", "district": "A-03"},
    constraints=["安全优先", "资源受限时按不可逆风险排序"],
)

EVIDENCE = [
    {
        "source": "samples/assets/urban/road_network.csv",
        "version": "v1.1",
        "content": "R-101 兴华路积水 30cm，超过 25cm 封路阈值，当前状态 closed",
    },
    {
        "source": "samples/assets/urban/drainage_facilities.csv",
        "version": "v1",
        "content": "DF-PS-01 泵站故障且无备用电源，DF-IN-101 雨水口反涌",
    },
]


def main() -> int:
    try:
        settings = settings_from_env()
    except ModelProviderError as exc:
        print(f"配置错误: {exc}")
        return 2

    if not settings.is_real:
        print(
            "跳过：未配置真实模型。"
            f"设置 {PROVIDER_ENV}=deepseek 与 {API_KEY_ENV} 后再运行；"
            "默认离线路径不受影响。"
        )
        return 0

    print(f"provider={settings.provider} model={settings.model_name}")
    try:
        decision = asyncio.run(generate_decision_live(REQUEST, EVIDENCE, settings))
    except ModelProviderError as exc:
        print(f"配置错误: {exc}")
        return 2
    except Exception as exc:  # noqa: BLE001 - surface provider/transport errors to the caller
        print(f"调用失败: {type(exc).__name__}: {exc}")
        return 1

    print(decision.model_dump_json(indent=2))
    print(
        "结果校验: "
        f"request_id={decision.request_id} "
        f"selected_option_id={decision.selected_option_id} "
        f"confidence={decision.confidence}"
    )
    if "--out" in sys.argv:
        out_index = sys.argv.index("--out") + 1
        if out_index < len(sys.argv):
            out_path = Path(sys.argv[out_index])
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(decision.model_dump_json(indent=2), encoding="utf-8")
            print(f"已固化证据: {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
