"""P8 之上的真实模型 A/B 评测入口。

默认只跑离线 provider（rule / test），不联网：

    $env:PYTHONPATH = "src"
    uv run --locked python scripts/llm_ab_eval.py --report docs/reports/p8-llm-ab-report.md

加真实模型（会消耗 API 额度）：

    $env:DECISION_LLM_PROVIDER = "deepseek"
    $env:DEEPSEEK_API_KEY      = "<你的 key>"
    uv run --group llm python scripts/llm_ab_eval.py --provider rule --provider deepseek
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from decision_poc.llm_ab import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
