"""把证据引用（`asset_id#locator`）解析成原文摘录，供决策任务构造使用。

为什么要单独一层
---------------
决策 Agent 拿到的是"第 1 优先动作：停止基坑开挖"，以及一条形如
`AS-A-03-emergency_plan-...#A-03/emergency_plan/v2.0#line:38` 的出处字符串。
**字符串本身不构成依据**：模型看不到"橙色预警期间应停止开挖、燃气每 2 小时检测"这句话，
就只能按选项文本里的表面阈值（30cm ≥ 25cm）自行改判——2026-11-06 平台侧 6 次复测里有 2 次
就是这么跑偏的。

解析保留权利边界：走 `UrbanToolkit.evidence_locate`，受限资产（部门规程）对未授权调用方
仍然取不到，返回 `None`，证据池退回"只有出处、没有原文"的旧行为，不会因此泄露正文。
"""

from __future__ import annotations

from typing import Any, Callable, Sequence

RefResolver = Callable[[str], "str | None"]

MAX_EXCERPT_CHARS = 200


def make_ref_resolver(
    caller_labels: Sequence[str] = ("ORG-EMERG",),
    *,
    toolkit: Any | None = None,
    max_chars: int = MAX_EXCERPT_CHARS,
) -> RefResolver:
    """构造一个"引用 → 原文摘录"的解析器；解析失败或无权访问时返回 None。"""

    tool = toolkit
    if tool is None:
        from .mcp_server import UrbanToolkit  # 延迟导入：离线路径不必须拉起数据集

        tool = UrbanToolkit()

    labels = [str(item) for item in caller_labels]

    def resolve(ref: str) -> str | None:
        try:
            result = tool._evidence_locate({"evidence_ref": ref, "caller_labels": labels})
        except Exception:  # noqa: BLE001 - 解析只是增强，任何异常都退回"不带原文"
            return None
        payload = result.model_dump(mode="json") if hasattr(result, "model_dump") else result
        if not isinstance(payload, dict) or payload.get("status") != "ok":
            return None
        data = payload.get("data") or {}
        excerpt = str(data.get("excerpt") or "").replace("\n", " ").strip()
        if not excerpt:
            return None
        return excerpt[:max_chars]

    return resolve


__all__ = ["MAX_EXCERPT_CHARS", "RefResolver", "make_ref_resolver"]
