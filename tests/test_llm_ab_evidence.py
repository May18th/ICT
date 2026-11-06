"""决策证据池的守卫：证据必须进池、进请求、进平台查询。

事故背景（2026-11-06）：平台 LLM 决策 Agent 连跑 3 次都没选中确定性推演的最优先项
（期望 opt-3=停止基坑开挖，实际 opt-2=封闭积水路段 ×2、opt-1 ×1）。排查结论不是"模型随机"，
而是**平台链路只把 `request` 发出去，证据池留在进程内**——模型只看到"水深 30cm + 四个选项文本"，
于是按选项自带的阈值（≥25cm）做了二次推理。同时证据池按 `pool[:8]` 截断，7~15 条
`action_priority` 把冲突、资源缺口与六跳结论全挤掉了。

这一组测试盯四件事，任何一件回退都会红：
1. 冲突与资源缺口在池子里**必有位置**（不能被执行排序挤掉）；
2. 六跳结论也在池子里（"为什么是这个排序"）；
3. 证据同时出现在 `request.context` 里（平台只看得到 request）；
4. 渲染成平台查询后确实带着证据（否则前三条只是自我安慰）。
"""

from __future__ import annotations

import pytest

from decision_poc.llm_ab import EVIDENCE_QUOTA, MAX_EVIDENCE, build_task
from decision_poc.nexent_transport import build_decision_query
from decision_poc.skills import SkillPipeline, SkillRuntime
from decision_poc.urban_eval import load_cases

CASES = {case.case_id: case for case in load_cases()}


@pytest.fixture(scope="module")
def task():
    case = CASES["N01"]
    report = SkillPipeline(SkillRuntime()).run(
        case.event_id, case.as_of, caller_labels=case.caller_labels, persist=False
    )
    return build_task(case, report)


def _sources(task) -> list[str]:
    return [str(item["source"]) for item in task.evidence]


def test_pool_reserves_slots_for_conflicts_and_gaps(task) -> None:
    """冲突与资源缺口必须进池：它们是"为什么不能那样做"，比排序更容易被挤掉。"""

    sources = _sources(task)
    conflicts = [s for s in sources if s.startswith("conflict:")]
    gaps = [s for s in sources if s.startswith("resource_gap:")]
    priorities = [s for s in sources if s.startswith("action_priority:")]

    assert conflicts, "冲突证据被挤掉了：决策节点看不到约束"
    assert gaps, "资源缺口被挤掉了：分配类动作会凭空成立"
    assert len(conflicts) <= EVIDENCE_QUOTA["conflict"]
    assert len(gaps) <= EVIDENCE_QUOTA["resource_gap"]
    assert len(priorities) <= EVIDENCE_QUOTA["action_priority"]
    assert len(sources) <= MAX_EVIDENCE


def test_pool_carries_stage_evidence(task) -> None:
    """六跳结论要在场：只给排序不给受影响对象/主体，模型没法解释"为什么先做这个"。"""

    sources = _sources(task)
    stage_sources = [s for s in sources if s.startswith("AS-")]
    assert stage_sources, "六跳结论的证据引用被挤掉了"
    # 「六跳结论」与「第 1 优先动作的原文」都取自 AS- 引用，两者各有配额
    budget = EVIDENCE_QUOTA["stage_evidence"] + EVIDENCE_QUOTA["top_action_evidence"]
    assert len(stage_sources) <= budget


def test_priority_six_is_capped_but_top_one_survives(task) -> None:
    """排序可以截断，但"第 1 优先"必须在场——那是决策答案本身。"""

    sources = _sources(task)
    assert "action_priority:1" in sources
    assert f"action_priority:{EVIDENCE_QUOTA['action_priority'] + 1}" not in sources


def test_resolver_replaces_ref_with_excerpt() -> None:
    """给了解析器就要看到原文：出处字符串本身不是依据。"""

    case = CASES["N01"]
    report = SkillPipeline(SkillRuntime()).run(
        case.event_id, case.as_of, caller_labels=case.caller_labels, persist=False
    )
    plain = build_task(case, report)
    resolved = build_task(case, report, resolve_ref=lambda ref: "【原文】基坑橙色预警期间停止开挖")

    plain_text = " ".join(str(item["content"]) for item in plain.evidence)
    resolved_text = " ".join(str(item["content"]) for item in resolved.evidence)

    assert "【原文】" not in plain_text, "不传解析器时不该凭空出现原文"
    assert "【原文】" in resolved_text, "传了解析器却没有原文，说明配额没留给第 1 优先动作"
    # 出处仍然要保留：原文与 ref 一起才是可核对的依据
    assert any(str(item["source"]).startswith("AS-") for item in resolved.evidence)


def test_evidence_travels_inside_the_request(task) -> None:
    """证据必须进 request.context：平台只发 request，留在进程内等于没给。"""

    context_evidence = task.request.context.get("evidence")
    assert isinstance(context_evidence, list) and context_evidence
    assert context_evidence == task.evidence, "context 里的证据与 task.evidence 必须是同一份"
    assert task.request.context.get("evidence_note")
    assert "action_priority:1" in str(task.request.context.get("rule", ""))


def test_platform_query_actually_carries_the_pool(task) -> None:
    """最后一道闸：渲染成北向查询后，证据还在（这是平台侧唯一能看到的文本）。"""

    query = build_decision_query(task.request.model_dump(mode="json"))
    assert "action_priority:1" in query
    assert "conflict:" in query
    assert "resource_gap:" in query
    assert "第1优先处置动作" in query or "第 1 优先处置动作" in query


def test_every_case_still_yields_non_empty_pool() -> None:
    """配额不能把别的用例饿死，也不能撑破上限。

    拒答用例不强制非空：I02 这类"as_of 早于工单上报"的用例，六跳结论本身就是那条
    "事件尚未上报"的依据，带上它反而让拒答有据可依；I01 没有任何依据则允许为空。
    """

    runtime = SkillRuntime()
    checked = 0
    for case_id, case in CASES.items():
        report = SkillPipeline(runtime).run(
            case.event_id, case.as_of, caller_labels=case.caller_labels, persist=False
        )
        built = build_task(case, report)
        assert len(built.evidence) <= MAX_EVIDENCE, case_id
        if built.expected_kind == "refuse":
            continue
        checked += 1
        assert built.evidence, f"{case_id} 证据池为空"
        if report.deduction is not None and report.deduction.action_order:
            # 有推演结论就必须带排序；权限受限（P01 这类 deduction=None）只能用事实兜底
            assert any(str(item["source"]).startswith("action_priority:") for item in built.evidence), case_id
    assert checked >= 5, "正常用例数量异常，检查用例集是否被改动"
