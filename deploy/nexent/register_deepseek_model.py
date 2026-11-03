"""Nexent 平台模型登记：把 DeepSeek 登记进模型设置页（不绑定 Agent、不占默认槽位）。

为什么走容器内 API 而不是浏览器：平台管理 API 用 JWT 鉴权，密钥只在 nexent-config 容器里；
与 setup_nexent.py / upload_skills.py 同一通道，幂等、可复核、不依赖前端交互。

用法（容器内）：
    .venv/bin/python /tmp/register_deepseek_model.py list
    .venv/bin/python /tmp/register_deepseek_model.py create     # 需环境变量 DEEPSEEK_API_KEY
    .venv/bin/python /tmp/register_deepseek_model.py healthcheck
    .venv/bin/python /tmp/register_deepseek_model.py verify

关键约束（与初赛材料口径一致）：
- 只登记，不切换任何 Agent 的绑定模型；
- skip_default_backfill=True，不得占用 10 个默认模型槽位中的任何一个；
- API key 只从环境变量读，输出一律脱敏。
"""

from __future__ import annotations

import json
import os
import sys

from platform_client import PlatformClient

DISPLAY_NAME = "DeepSeek-Chat"
MODEL_NAME = "deepseek-chat"
MODEL_TYPE = "llm"
BASE_URL = "https://api.deepseek.com/v1"
MODEL_FACTORY = "OpenAI-API-Compatible"
MAX_TOKENS = 8192


def _mask(value: str | None) -> str:
    if not value:
        return "<empty>"
    return f"{value[:6]}...{value[-4:]} (len={len(value)})"


def show_models(client: PlatformClient) -> list[dict]:
    status, body = client.call("/api/model/list")
    print(f"[list] http={status}")
    if status != 200:
        print(json.dumps(body, ensure_ascii=False)[:500])
        raise SystemExit(1)
    models = body.get("data") or []
    for m in models:
        print(
            f"  - display_name={m.get('display_name')!r} model_name={m.get('model_name')!r} "
            f"type={m.get('model_type')!r} factory={m.get('model_factory')!r} "
            f"base_url={m.get('base_url')!r} connect_status={m.get('connect_status')!r}"
        )
    return models


def cmd_list(client: PlatformClient) -> None:
    show_models(client)


def cmd_create(client: PlatformClient) -> None:
    models = show_models(client)
    names = {m.get("display_name") for m in models}
    if DISPLAY_NAME in names:
        print(f"[create] {DISPLAY_NAME} 已存在，跳过（幂等）")
        return

    api_key = os.getenv("DEEPSEEK_API_KEY")
    if not api_key:
        raise SystemExit("[create] 缺环境变量 DEEPSEEK_API_KEY，拒绝创建")
    print(f"[create] api_key={_mask(api_key)}")

    payload = {
        "model_factory": MODEL_FACTORY,
        "model_name": MODEL_NAME,
        "model_type": MODEL_TYPE,
        "api_key": api_key,
        "base_url": BASE_URL,
        "display_name": DISPLAY_NAME,
        "max_tokens": MAX_TOKENS,
        # 红线：登记不占位。默认模型槽位（主 LLM 等 10 个）保持原样。
        "skip_default_backfill": True,
    }
    status, body = client.call("/api/model/create", method="POST", payload=payload)
    print(f"[create] http={status} resp={json.dumps(body, ensure_ascii=False)[:400]}")
    if status != 200:
        raise SystemExit(1)
    print(
        "[create] 注意 auto_configured_defaults："
        f"{body.get('auto_configured_defaults') if isinstance(body, dict) else None}"
    )


def cmd_healthcheck(client: PlatformClient) -> None:
    status, body = client.call(
        f"/api/model/healthcheck?display_name={DISPLAY_NAME}&model_type={MODEL_TYPE}",
        method="POST",
    )
    print(f"[healthcheck] http={status} resp={json.dumps(body, ensure_ascii=False)[:400]}")


def cmd_verify(client: PlatformClient) -> None:
    models = show_models(client)
    target = [m for m in models if m.get("display_name") == DISPLAY_NAME]
    if not target:
        raise SystemExit("[verify] FAIL：未找到 DeepSeek-Chat")
    record = target[0]
    print(f"[verify] record: {json.dumps(record, ensure_ascii=False)[:600]}")
    llm_models = [m for m in models if m.get("model_type") == MODEL_TYPE]
    print(f"[verify] LLM 类模型共 {len(llm_models)} 个：{[m.get('display_name') for m in llm_models]}")
    print(
        "[verify] 人工核对项：模型设置页「已配置 n/10」的默认槽位不应变化；"
        "主 Agent 的绑定模型应仍为 openPangu-2.0-Pro。"
    )


def main() -> None:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    client = PlatformClient()
    {"list": cmd_list, "create": cmd_create,
     "healthcheck": cmd_healthcheck, "verify": cmd_verify}[cmd](client)


if __name__ == "__main__":
    main()
