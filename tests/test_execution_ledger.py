"""执行账本：状态机、幂等、乐观锁、会签、关单，以及"不动冻结台账"。

这组测试守的是**拒绝**而不是"能写进去"。执行账本一旦放宽，
页面上就会看到一张没人点过却显示"已完成"的卡——那正是要避免的假闭环。
所以每条拒绝路径都得有用例：跳步、版本过期、会签没齐、卡不存在、重复提交。
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from decision_poc import execution_ledger as ledger


def _cards() -> dict[str, dict]:
    """两张卡：01 不需要会签，02 需要道路与交警双方会签，同属一张工单。"""
    return {
        "AC-T-01": {
            "card_id": "AC-T-01",
            "order_ids": ["WO-TEST-001"],
            "co_sign_required": [],
            "claim_ids": ["CL-S6-action-01"],
            "evidence_refs": ["AS-#A-03/work_order/v1#row:2"],
        },
        "AC-T-02": {
            "card_id": "AC-T-02",
            "order_ids": ["WO-TEST-001"],
            "co_sign_required": ["ORG-ROAD", "ORG-TRAFFIC"],
            "claim_ids": ["CL-S6-action-02"],
            "evidence_refs": ["AS-#A-03/work_order/v1#row:3"],
        },
    }


@pytest.fixture()
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, dict]:
    monkeypatch.setenv(ledger.EXECUTION_DIR_ENV, str(tmp_path / "execution"))
    return _cards()


def _payload(card_id: str, to_state: str, *, version: int = 0, action: str = "transition", **extra) -> dict:
    body = {
        "event_id": f"REQ-{card_id}-{to_state}-{version}",
        "card_id": card_id,
        "action": action,
        "to_state": to_state,
        "expected_version": version,
    }
    body.update(extra)
    return body


def _state(card_id: str, cards: dict) -> str:
    return ledger.current_state(cards)["cards"][card_id]["state"]


# --- 状态机 ---------------------------------------------------------------


def test_initial_projection_is_all_pending(env: dict) -> None:
    state = ledger.current_state(env)
    assert {row["state"] for row in state["cards"].values()} == {"pending"}
    assert state["events"] == 0


def test_happy_path_pending_running_done(env: dict) -> None:
    ledger.append_event(_payload("AC-T-01", "running"), cards=env)
    assert _state("AC-T-01", env) == "running"
    ledger.append_event(_payload("AC-T-01", "done", version=1), cards=env)
    assert _state("AC-T-01", env) == "done"
    events = ledger.read_events()
    assert [event["to_state"] for event in events] == ["running", "done"]
    assert all(event["actor_org"] == ledger.DEFAULT_ACTOR["actor_org"] for event in events)


def test_illegal_transition_is_rejected(env: dict) -> None:
    """跳步不认：pending 直接 done 必须拒，且不留下任何事件。"""
    with pytest.raises(ledger.LedgerError) as exc:
        ledger.append_event(_payload("AC-T-01", "done"), cards=env)
    assert exc.value.status == 409
    assert exc.value.code == "illegal_transition"
    assert exc.value.detail["allowed"] == ["running", "cancelled"]
    assert ledger.read_events() == []


def test_done_is_terminal(env: dict) -> None:
    """已完成的卡不会再动：要么被幂等吸收，要么被判非法迁移，状态都不许变。

    幂等键粒度是 `(card_id, to_state)`，所以重开请求多半会以 `duplicate` 返回——
    这属于契约已知边界（见 `idempotency_key` 的说明），断言只要求"状态不变、不追加事件"。
    """
    ledger.append_event(_payload("AC-T-01", "running"), cards=env)
    ledger.append_event(_payload("AC-T-01", "done", version=1), cards=env)
    before = len(ledger.read_events())
    try:
        result = ledger.append_event(_payload("AC-T-01", "running", version=2), cards=env)
        assert result["status"] == "duplicate"
    except ledger.LedgerError as exc:
        assert exc.code in {"illegal_transition", "version_conflict"}
    assert _state("AC-T-01", env) == "done"
    assert len(ledger.read_events()) == before


# --- 幂等与并发 -----------------------------------------------------------


def test_duplicate_submission_does_not_append(env: dict) -> None:
    first = ledger.append_event(_payload("AC-T-01", "running"), cards=env)
    second = ledger.append_event(_payload("AC-T-01", "running"), cards=env)
    assert first["status"] == "ok"
    assert second["status"] == "duplicate"
    assert len(ledger.read_events()) == 1


def test_idempotency_key_matches_contract(env: dict) -> None:
    ledger.append_event(_payload("AC-T-01", "running"), cards=env)
    event = ledger.read_events()[0]
    expected = hashlib.sha256("AC-T-01|running".encode("utf-8")).hexdigest()[:32]
    assert event["idempotency_key"] == expected == ledger.idempotency_key("AC-T-01", "running")


def test_version_conflict_returns_409(env: dict) -> None:
    ledger.append_event(_payload("AC-T-01", "running"), cards=env)
    with pytest.raises(ledger.LedgerError) as exc:
        # 前端拿着旧版本（0）再提交，必须被挡
        ledger.append_event(_payload("AC-T-01", "done", version=0), cards=env)
    assert exc.value.status == 409
    assert exc.value.code == "version_conflict"
    assert exc.value.detail["current_version"] == 1


def test_unknown_card_is_404(env: dict) -> None:
    with pytest.raises(ledger.LedgerError) as exc:
        ledger.append_event(_payload("AC-NOT-EXIST", "running"), cards=env)
    assert exc.value.status == 404
    assert exc.value.code == "card_not_found"


# --- 会签 -----------------------------------------------------------------


def test_co_sign_blocks_done_until_all_signed(env: dict) -> None:
    ledger.append_event(_payload("AC-T-02", "running"), cards=env)
    ledger.append_event(_payload("AC-T-02", "needs_confirm", version=1), cards=env)

    with pytest.raises(ledger.LedgerError) as exc:
        ledger.append_event(_payload("AC-T-02", "done", version=2), cards=env)
    assert exc.value.code == "co_sign_incomplete"
    assert exc.value.detail["missing"] == ["ORG-ROAD", "ORG-TRAFFIC"]

    # 只签一家还不够
    ledger.append_event(_payload("AC-T-02", "", version=2, action="sign"), cards=env, actor={"actor_org": "ORG-ROAD"})
    with pytest.raises(ledger.LedgerError) as exc:
        ledger.append_event(_payload("AC-T-02", "done", version=2), cards=env)
    assert exc.value.detail["missing"] == ["ORG-TRAFFIC"]

    ledger.append_event(_payload("AC-T-02", "", version=2, action="sign"), cards=env, actor={"actor_org": "ORG-TRAFFIC"})
    ledger.append_event(_payload("AC-T-02", "done", version=2), cards=env)
    assert _state("AC-T-02", env) == "done"


def test_sign_requires_needs_confirm_state(env: dict) -> None:
    ledger.append_event(_payload("AC-T-02", "running"), cards=env)
    with pytest.raises(ledger.LedgerError) as exc:
        ledger.append_event(_payload("AC-T-02", "", version=1, action="sign"), cards=env)
    assert exc.value.code == "sign_not_required"


def test_repeated_signature_is_duplicate(env: dict) -> None:
    ledger.append_event(_payload("AC-T-02", "running"), cards=env)
    ledger.append_event(_payload("AC-T-02", "needs_confirm", version=1), cards=env)
    ledger.append_event(_payload("AC-T-02", "", version=2, action="sign"), cards=env, actor={"actor_org": "ORG-ROAD"})
    again = ledger.append_event(_payload("AC-T-02", "", version=2, action="sign"), cards=env, actor={"actor_org": "ORG-ROAD"})
    assert again["status"] == "duplicate"
    assert again["code"] == "already_signed"


# --- 工单关闭 -------------------------------------------------------------


def test_close_order_requires_all_cards_done(env: dict) -> None:
    with pytest.raises(ledger.LedgerError) as exc:
        ledger.append_event(_payload("AC-T-01", "", version=0, action="close_order", order_id="WO-TEST-001"), cards=env)
    assert exc.value.code == "card_not_done"
    assert len(exc.value.detail["unfinished"]) == 2


def test_close_order_after_all_done_then_duplicate(env: dict) -> None:
    ledger.append_event(_payload("AC-T-01", "running"), cards=env)
    ledger.append_event(_payload("AC-T-01", "done", version=1), cards=env)
    ledger.append_event(_payload("AC-T-02", "running"), cards=env)
    ledger.append_event(_payload("AC-T-02", "needs_confirm", version=1), cards=env)
    for org in ("ORG-ROAD", "ORG-TRAFFIC"):
        ledger.append_event(_payload("AC-T-02", "", version=2, action="sign"), cards=env, actor={"actor_org": org})
    ledger.append_event(_payload("AC-T-02", "done", version=2), cards=env)

    closed = ledger.append_event(
        _payload("AC-T-01", "", version=2, action="close_order", order_id="WO-TEST-001", note="人工确认结案"),
        cards=env,
    )
    assert closed["status"] == "order_closed"
    again = ledger.append_event(
        _payload("AC-T-01", "", version=2, action="close_order", order_id="WO-TEST-001"),
        cards=env,
    )
    assert again["status"] == "duplicate"


# --- 账本自身 -------------------------------------------------------------


def test_state_json_is_rebuildable(env: dict) -> None:
    """`state.json` 只是缓存：删掉后必须能从事件重放出同一个结果。"""
    ledger.append_event(_payload("AC-T-01", "running"), cards=env)
    first = ledger.current_state(env)
    ledger.state_path().unlink()
    rebuilt = ledger.current_state(env)
    assert first == rebuilt
    assert ledger.state_path().is_file()


def test_ledger_never_touches_work_order_csv(env: dict) -> None:
    """冻结台账一个字节都不能变——写回只写账本。"""
    csv_path = Path(__file__).resolve().parents[1] / "samples" / "assets" / "urban" / "work_orders.csv"
    before = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    ledger.append_event(_payload("AC-T-01", "running"), cards=env)
    ledger.append_event(_payload("AC-T-01", "done", version=1), cards=env)
    after = hashlib.sha256(csv_path.read_bytes()).hexdigest()
    assert before == after


def test_actor_cannot_be_overridden_by_payload(env: dict) -> None:
    """前端传 actor_org 一律忽略：审计身份必须由服务端给。"""
    ledger.append_event(
        _payload("AC-T-01", "running", actor_org="ORG-FAKE", actor_role="伪造角色"),
        cards=env,
        actor={"actor_org": "ORG-DRAIN", "actor_role": "排水值班（演示）"},
    )
    event = ledger.read_events()[0]
    assert event["actor_org"] == "ORG-DRAIN"
    assert event["actor_role"] == "排水值班（演示）"


def test_missing_required_field_is_400(env: dict) -> None:
    with pytest.raises(ledger.LedgerError) as exc:
        ledger.append_event({"action": "transition", "card_id": "AC-T-01"}, cards=env)
    assert exc.value.status == 400
    assert exc.value.code == "missing_field"


# --- 计划 vs 实际 ---------------------------------------------------------


def test_plan_compare_counts_only_real_ledger_state(env: dict) -> None:
    cards = [
        {"card_id": "AC-T-01", "planned_state": "done", "order_ids": ["WO-TEST-001"]},
        {"card_id": "AC-T-02", "planned_state": "done", "order_ids": ["WO-TEST-001"]},
    ]
    projected = ledger.project([], env)
    plan = ledger.compare_plan(cards, projected)
    assert plan["planned_done"] == 2
    assert plan["actual_done"] == 0
    assert plan["delta"] == -2

    ledger.append_event(_payload("AC-T-01", "running"), cards=env)
    ledger.append_event(_payload("AC-T-01", "done", version=1), cards=env)
    plan = ledger.compare_plan(cards, ledger.project(ledger.read_events(), env))
    assert plan["actual_done"] == 1
    assert plan["behind"] == ["AC-T-02"]


# --- 导出 -----------------------------------------------------------------


def test_export_snapshot_scrubs_personal_fields(env: dict) -> None:
    ledger.append_event(
        _payload("AC-T-01", "running", note="张三到现场"),
        cards=env,
        actor={"actor_org": "ORG-DRAIN", "actor_role": "排水值班（王五）"},
    )
    target = Path(env and ledger.execution_dir()) / "snap.json"
    path = ledger.export_snapshot(cards=env, target=target)
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["schema_version"] == "execution-snapshot-v1"
    event = payload["events"][0]
    assert event["actor_role"] == "（已脱敏）"
    assert event["note"] == ""
    assert event["actor_org"] == "ORG-DRAIN"  # 部门代码保留，用于复现
    assert "张三" not in path.read_text(encoding="utf-8")


# --- HTTP 端点 ------------------------------------------------------------


def _route(path: str, method: str = "GET", body: dict | None = None):
    from decision_poc.web_demo import route

    raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    status, content_type, payload = route(path, method, raw)
    return status, json.loads(payload)


def test_execution_endpoint_round_trip(env: dict) -> None:
    """真实卡片走一遍端点：GET 看计划/实际，POST 推一步，再 GET 实际数 +1。"""
    status, view = _route("/api/execution")
    assert status == 200
    assert view["plan"]["planned_done"] >= 1
    assert view["plan"]["actual_done"] == 0
    assert view["cards"] and view["cards"][0]["planned_label"]
    assert "演示" in view["source"]

    card = view["cards"][0]
    status, result = _route(
        "/api/execution/events",
        "POST",
        {
            "event_id": "REQ-1",
            "card_id": card["card_id"],
            "action": "transition",
            "to_state": "running",
            "expected_version": 0,
        },
    )
    assert status == 201
    assert result["status"] == "ok"
    assert result["event"]["actor_org"] == ledger.DEFAULT_ACTOR["actor_org"]

    status, again = _route("/api/execution")
    assert again["plan"]["actual_done"] == 0  # 只推到 running，还没完成
    assert {row["card_id"]: row for row in again["cards"]}[card["card_id"]]["state"] == "running"


def test_execution_endpoint_reports_rejection(env: dict) -> None:
    """非法迁移必须带着 code 与 detail 返回，前端才能说清"为什么不让点"。"""
    _, view = _route("/api/execution")
    card = view["cards"][0]
    status, result = _route(
        "/api/execution/events",
        "POST",
        {
            "event_id": "REQ-2",
            "card_id": card["card_id"],
            "action": "transition",
            "to_state": "done",  # 跳步
            "expected_version": 0,
        },
    )
    assert status == 409
    assert result["code"] == "illegal_transition"
    assert result["detail"]["allowed"] == ["running", "cancelled"]
    assert ledger.read_events() == []
