"""P5 MCP server: protocol shape and tool contract.

Protocol tests drive `McpServer.handle()` directly (no subprocess) so a failure
points at the protocol, not at process plumbing. One subprocess test exists at
the end because "it works when spawned" is a different claim from "it works when
called".
"""

from __future__ import annotations

import json

import pytest

from decision_poc.mcp_server import (
    INVALID_PARAMS,
    INVALID_REQUEST,
    METHOD_NOT_FOUND,
    McpServer,
    SERVER_NAME,
    UrbanToolkit,
    parse_locator,
    serve_stdio,
)
from decision_poc.provenance import Claim, ClaimKind, build_ledger
from decision_poc.semantic import build_urban_semantic_store

AS_OF = "2026-10-29T12:00:00+08:00"


@pytest.fixture(scope="module")
def toolkit() -> UrbanToolkit:
    return UrbanToolkit()


@pytest.fixture(scope="module")
def server(toolkit: UrbanToolkit) -> McpServer:
    return McpServer(toolkit)


def _call(server: McpServer, name: str, arguments: dict, request_id: int = 7) -> dict:
    response = server.handle(
        {"jsonrpc": "2.0", "id": request_id, "method": "tools/call", "params": {"name": name, "arguments": arguments}}
    )
    assert "error" not in response, response.get("error")
    payload = json.loads(response["result"]["content"][0]["text"])
    return payload


# --- protocol ------------------------------------------------------------


def test_initialize_handshake(server: McpServer) -> None:
    response = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    result = response["result"]
    assert result["serverInfo"]["name"] == SERVER_NAME
    assert "tools" in result["capabilities"]
    assert result["protocolVersion"]


def test_notification_gets_no_response(server: McpServer) -> None:
    assert server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_shape(server: McpServer) -> None:
    tools = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
    names = [tool["name"] for tool in tools]
    assert names == [
        "semantic_query",
        "twin_neighbors",
        "evidence_locate",
        "claim_trace",
        "provenance_verify",
        "event_deduce",
        "counterfactual_run",
        "conflict_check",
    ]
    assert len(names) == 8, "P5 的 7 个工具 + P6 反事实的 1 个"
    for tool in tools:
        assert tool["description"]
        schema = tool["inputSchema"]
        assert schema["type"] == "object"
        assert schema["properties"], "每个工具都必须声明参数结构"
    # provenance_verify 的参数全可选（不带 decision_id 即整链校验）
    by_name = {tool["name"]: tool for tool in tools}
    assert by_name["provenance_verify"]["inputSchema"].get("required", []) == []
    assert by_name["semantic_query"]["inputSchema"]["required"] == ["query_id", "as_of", "seed_ids"]


def test_unknown_method_is_method_not_found(server: McpServer) -> None:
    response = server.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/delete"})
    assert response["error"]["code"] == METHOD_NOT_FOUND


def test_bad_jsonrpc_version_is_rejected(server: McpServer) -> None:
    response = server.handle({"jsonrpc": "1.0", "id": 4, "method": "ping"})
    assert response["error"]["code"] == INVALID_REQUEST


def test_unknown_tool_is_invalid_params(server: McpServer) -> None:
    response = server.handle(
        {"jsonrpc": "2.0", "id": 5, "method": "tools/call", "params": {"name": "nope", "arguments": {}}}
    )
    assert response["error"]["code"] == INVALID_PARAMS


def test_stdio_loop_skips_blank_lines(toolkit: UrbanToolkit) -> None:
    import io

    stdin = io.StringIO('\n{"jsonrpc":"2.0","id":1,"method":"ping"}\n\n')
    stdout = io.StringIO()
    serve_stdio(toolkit, stdin=stdin, stdout=stdout)
    lines = [line for line in stdout.getvalue().splitlines() if line.strip()]
    assert len(lines) == 1
    assert json.loads(lines[0])["result"] == {}


def test_stdio_loop_reports_parse_error(toolkit: UrbanToolkit) -> None:
    import io

    stdin = io.StringIO("not json\n")
    stdout = io.StringIO()
    serve_stdio(toolkit, stdin=stdin, stdout=stdout)
    payload = json.loads(stdout.getvalue().strip())
    assert payload["error"]["code"] == -32700


# --- locator parsing -----------------------------------------------------


def test_parse_locator_forms() -> None:
    assert parse_locator("A-03/x/v1#row:4") == ("row", {"start": 4, "end": 4})
    assert parse_locator("A-03/x/v1#line:33-35") == ("line", {"start": 33, "end": 35})
    assert parse_locator("A-03/x/v1#region:1,2,3,4") == ("region", {"bbox": [1, 2, 3, 4]})


def test_parse_locator_rejects_garbage() -> None:
    for bad in ["no-hash", "A-03/x/v1#row", "A-03/x/v1#row:0", "A-03/x/v1#row:9-3", "A-03/x/v1#region:1,2,3", "A-03/x/v1#page:2"]:
        with pytest.raises(ValueError):
            parse_locator(bad)


# --- semantic tools ------------------------------------------------------


def test_semantic_query_returns_snapshot_anchor(toolkit: UrbanToolkit, server: McpServer) -> None:
    payload = _call(
        server,
        "semantic_query",
        {
            "query_id": "q1",
            "as_of": AS_OF,
            "seed_ids": ["TW-incident-WO-20261029-007"],
            "max_hops": 2,
            "caller_labels": ["ORG-EMERG"],
            "block_expansion_through": ["region"],
        },
    )
    assert payload["status"] == "ok"
    assert payload["data"]["snapshot_hash"] == toolkit.store.snapshot_hash()
    assert payload["data"]["knowledge_version"] == "kb-v1.0.0"
    assert payload["data"]["triple_count"] > 0


def test_semantic_query_bad_arguments_is_validation_failure(server: McpServer) -> None:
    payload = _call(server, "semantic_query", {"query_id": "q1", "as_of": "2026-10-29T12:00:00", "seed_ids": []})
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "VALIDATION_FAILED"
    assert payload["error"]["retryable"] is False


def test_semantic_query_unknown_seed_is_error(server: McpServer) -> None:
    payload = _call(server, "semantic_query", {"query_id": "q1", "as_of": AS_OF, "seed_ids": ["TW-ghost"]})
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "NOT_FOUND"


def test_twin_neighbors_unknown_twin_is_not_found(server: McpServer) -> None:
    payload = _call(server, "twin_neighbors", {"twin_id": "TW-ghost", "as_of": AS_OF})
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "NOT_FOUND"


def test_twin_neighbors_returns_triples(toolkit: UrbanToolkit, server: McpServer) -> None:
    payload = _call(server, "twin_neighbors", {"twin_id": "TW-pump_station-DF-PS-01", "as_of": AS_OF})
    assert payload["status"] == "ok"
    assert payload["data"]["twin_id"] == "TW-pump_station-DF-PS-01"
    assert payload["data"]["triples"]


# --- evidence locate -----------------------------------------------------


def _work_order_ref(toolkit: UrbanToolkit, suffix: str) -> str:
    """asset_id 是内容寻址的：改一个字符就换 id，所以测试自己算，别抄字面量。"""
    asset = next(a for a in toolkit._assets.values() if a.file_name == "work_orders.csv")
    return f"{asset.asset_id}#A-03/work_order/v1#{suffix}"


def test_evidence_locate_row_returns_parsed_fields(
    toolkit: UrbanToolkit, server: McpServer
) -> None:
    payload = _call(
        server,
        "evidence_locate",
        {"evidence_ref": _work_order_ref(toolkit, "row:2")},
    )
    assert payload["status"] == "ok"
    assert payload["data"]["row"]["order_id"] == "WO-20261029-007"
    assert "积水" in payload["data"]["excerpt"]
    assert payload["data"]["asset"]["checksum_sha256"]


def test_evidence_locate_line_span(server: McpServer) -> None:
    payload = _call(
        server,
        "evidence_locate",
        {"evidence_ref": "AS-A-03-emergency_plan-cbaa17ca#A-03/emergency_plan/v1.0#line:33-35"},
    )
    assert payload["status"] == "ok"
    assert payload["data"]["span"]["start"] == 33
    assert "4.1" in payload["data"]["excerpt"]


def test_evidence_locate_region(toolkit: UrbanToolkit, server: McpServer) -> None:
    map_asset = next(a for a in toolkit._assets.values() if a.file_name == "pipeline_map.png")
    payload = _call(
        server,
        "evidence_locate",
        {"evidence_ref": f"{map_asset.asset_id}#A-03/schematic_map/v1#region:250,245,220,30"},
    )
    assert payload["status"] == "ok"
    assert payload["data"]["region"]["label"] == "积水区"


def test_evidence_locate_rejects_locator_from_another_asset(toolkit: UrbanToolkit, server: McpServer) -> None:
    """伪引用：asset_id 与 locator 前缀对不上，必须拦。"""
    map_asset = next(a for a in toolkit._assets.values() if a.file_name == "pipeline_map.png")
    payload = _call(
        server,
        "evidence_locate",
        {"evidence_ref": f"{map_asset.asset_id}#A-03/drainage_facility/v1#row:2"},
    )
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "VALIDATION_FAILED"


def test_evidence_locate_unknown_asset(server: McpServer) -> None:
    payload = _call(server, "evidence_locate", {"evidence_ref": "AS-ghost#A-03/x/v1#row:1"})
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "NOT_FOUND"


def test_evidence_locate_out_of_range(toolkit: UrbanToolkit, server: McpServer) -> None:
    payload = _call(
        server,
        "evidence_locate",
        {"evidence_ref": _work_order_ref(toolkit, "row:999")},
    )
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "VALIDATION_FAILED"


# --- provenance ----------------------------------------------------------


def test_provenance_verify_on_empty_ledger(server: McpServer) -> None:
    payload = _call(server, "provenance_verify", {})
    assert payload["status"] == "ok"
    assert payload["data"]["verify"]["ok"] is True
    assert payload["data"]["record_count"] == 0


def test_provenance_verify_replays_known_decision(tmp_path) -> None:
    store = build_urban_semantic_store()
    triple = next(t for t in store.triples() if t.predicate.value == "maintained_by")
    ledger = build_ledger()
    ledger.register(
        [
            Claim(
                claim_id="CL-1",
                text="某设施由某部门养护",
                kind=ClaimKind.FACT,
                evidence_refs=[triple.evidence_refs[0]],
                produced_by="city_extract@1.0",
            )
        ]
    )
    ledger.append(decision_id="DEC-1", claim_ids=["CL-1"], semantic_snapshot_hash=store.snapshot_hash())
    ledger.save(tmp_path / "pv")

    served = McpServer(UrbanToolkit(ledger_dir=str(tmp_path / "pv")))
    payload = _call(served, "provenance_verify", {"decision_id": "DEC-1"})
    assert payload["status"] == "ok"
    assert payload["data"]["replay"]["decision_id"] == "DEC-1"
    assert payload["data"]["replay"]["asset_fingerprints"]


def test_provenance_verify_unknown_decision(server: McpServer) -> None:
    payload = _call(server, "provenance_verify", {"decision_id": "DEC-ghost"})
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "NOT_FOUND"


# --- placeholders --------------------------------------------------------


EVENT = "WO-20261029-007"
EVENT_AS_OF = "2026-10-29T09:40:00+08:00"


def test_event_deduce_returns_six_hops(server: McpServer) -> None:
    payload = _call(
        server,
        "event_deduce",
        {"event_id": EVENT, "as_of": EVENT_AS_OF, "caller_labels": ["ORG-EMERG"]},
    )
    assert payload["status"] == "ok"
    data = payload["data"]
    assert [step["stage"] for step in data["steps"][:3]] == ["observed", "affected", "owner"]
    assert {conflict["kind"] for conflict in data["conflicts"]} == {"overlap", "gap", "contention"}
    assert data["resource_gaps"][0]["gap"] == 1
    assert data["snapshot_hash"] == payload["data"]["root"]["snapshot_hash"]


def test_event_deduce_unknown_event(server: McpServer) -> None:
    payload = _call(
        server, "event_deduce", {"event_id": "WO-ghost", "as_of": EVENT_AS_OF, "caller_labels": []}
    )
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "NOT_FOUND"


def test_event_deduce_refuses_when_evidence_is_not_visible(server: McpServer) -> None:
    """路政身份看不到燃气规程，就不能拿走基于它的推演结论。"""
    payload = _call(
        server,
        "event_deduce",
        {"event_id": EVENT, "as_of": EVENT_AS_OF, "caller_labels": ["ORG-ROAD"]},
    )
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "PERMISSION_DENIED"
    assert payload["data"]["blocked_assets"]


def test_conflict_check_returns_three_kinds(server: McpServer) -> None:
    payload = _call(
        server,
        "conflict_check",
        {"event_id": EVENT, "as_of": EVENT_AS_OF, "caller_labels": ["ORG-EMERG"]},
    )
    assert payload["status"] == "ok"
    assert payload["data"]["kinds"] == ["contention", "gap", "overlap"]
    assert payload["data"]["suggested_owner"]["DF-PS-01"] == "待明确"


# --- real process --------------------------------------------------------


def test_server_runs_as_subprocess() -> None:
    """"能被 spawn 起来"是另一个断言：编码、缓冲、退出码都得对。"""
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    requests = [
        {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {
                "name": "semantic_query",
                "arguments": {
                    "query_id": "q1",
                    "as_of": "2026-10-29T12:00:00+08:00",
                    "seed_ids": ["TW-pump_station-DF-PS-01"],
                    "max_hops": 1,
                    "caller_labels": ["ORG-EMERG"],
                },
            },
        },
    ]
    stdin = "\n".join(json.dumps(item, ensure_ascii=False) for item in requests) + "\n"
    completed = subprocess.run(
        [sys.executable, "-m", "decision_poc.mcp_server"],
        input=stdin,
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(root),
        env={**__import__("os").environ, "PYTHONPATH": str(root / "src")},
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    lines = [json.loads(line) for line in completed.stdout.splitlines() if line.strip()]
    assert [item["id"] for item in lines] == [1, 2, 3]
    assert lines[0]["result"]["serverInfo"]["name"] == SERVER_NAME
    assert len(lines[1]["result"]["tools"]) == 8
    payload = json.loads(lines[2]["result"]["content"][0]["text"])
    assert payload["status"] == "ok"
    assert payload["data"]["triple_count"] > 0


# --- permission and observability ---------------------------------------


def test_evidence_locate_respects_asset_visibility(toolkit: UrbanToolkit, server: McpServer) -> None:
    """受限规程的正文不能因为调用方知道 ref 就被取走。"""
    asset = next(a for a in toolkit._assets.values() if "procedure_waterlogging" in a.file_name)
    ref = f"{asset.asset_id}#{asset.locator}#line:1"

    denied = _call(server, "evidence_locate", {"evidence_ref": ref, "caller_labels": []})
    assert denied["status"] == "error"
    assert denied["error"]["code"] == "PERMISSION_DENIED"
    assert denied["data"]["required_labels"] == ["ORG-DRAIN", "ORG-EMERG"]

    allowed = _call(server, "evidence_locate", {"evidence_ref": ref, "caller_labels": ["ORG-DRAIN"]})
    assert allowed["status"] == "ok"
    assert "道路积水处置作业指引" in allowed["data"]["excerpt"]


def test_trace_id_is_propagated_and_logged(toolkit: UrbanToolkit, server: McpServer) -> None:
    response = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 11,
            "method": "tools/call",
            "params": {
                "name": "semantic_query",
                "arguments": {"query_id": "q1", "as_of": AS_OF, "seed_ids": ["TW-pump_station-DF-PS-01"]},
                "trace_id": "TRACE-42",
            },
        }
    )
    payload = json.loads(response["result"]["content"][0]["text"])
    assert payload["context"]["trace_id"] == "TRACE-42"
    assert toolkit.logs[-1]["trace_id"] == "TRACE-42"
    assert toolkit.logs[-1]["tool"] == "semantic_query"
    assert toolkit.logs[-1]["snapshot_hash"] == toolkit.store.snapshot_hash()


def test_tool_log_written_to_disk(tmp_path, toolkit: UrbanToolkit) -> None:
    from decision_poc.mcp_server import UrbanToolkit as Toolkit

    logged = Toolkit(log_path=str(tmp_path / "calls.jsonl"))
    logged.call("provenance_verify", {})
    lines = (tmp_path / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["tool"] == "provenance_verify"
    assert entry["status"] == "ok"


def test_claim_trace_unknown_claim(server: McpServer) -> None:
    payload = _call(server, "claim_trace", {"claim_id": "CL-ghost"})
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "NOT_FOUND"


def test_claim_trace_returns_located_sources(tmp_path) -> None:
    store = build_urban_semantic_store()
    triple = next(t for t in store.triples() if t.predicate.value == "maintained_by")
    ledger = build_ledger()
    ledger.register(
        [
            Claim(
                claim_id="CL-t",
                text="设施养护单位",
                kind=ClaimKind.FACT,
                evidence_refs=[triple.evidence_refs[0]],
                produced_by="city_extract@1.0",
            )
        ]
    )
    ledger.append(decision_id="DEC-t", claim_ids=["CL-t"], semantic_snapshot_hash=store.snapshot_hash())
    ledger.save(tmp_path / "pv")

    served = McpServer(UrbanToolkit(ledger_dir=str(tmp_path / "pv")))
    payload = _call(served, "claim_trace", {"claim_id": "CL-t"})
    assert payload["status"] == "ok"
    assert payload["data"]["decision_ids"] == ["DEC-t"]
    assert payload["data"]["assets"][0]["fingerprint"]
    assert payload["data"]["semantic_snapshot_hash"] == store.snapshot_hash()


# --- counterfactual ------------------------------------------------------


def test_counterfactual_shrinks_inventory_and_names_a_priority(server: McpServer) -> None:
    payload = _call(
        server,
        "counterfactual_run",
        {"case_id": "CF2", "event_id": EVENT, "as_of": EVENT_AS_OF, "caller_labels": ["ORG-EMERG"]},
    )
    assert payload["status"] == "ok"
    data = payload["data"]
    assert data["diff"]["gaps_delta"]["RES-PUMP-M"] == 4
    assert data["baseline"]["inventory"] == 6
    assert data["intervened"]["inventory"] == 2
    assert data["priority"]["recommended"] == "A-03"
    assert "40 分钟" in data["priority"]["cross_district_note"]


def test_counterfactual_rejects_unknown_case(server: McpServer) -> None:
    payload = _call(
        server,
        "counterfactual_run",
        {"case_id": "CF9", "event_id": EVENT, "as_of": EVENT_AS_OF, "caller_labels": []},
    )
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "NOT_FOUND"
    assert payload["data"]["known"] == ["CF1", "CF2", "CF3"]


def test_counterfactual_refuses_when_the_baseline_is_invisible(server: McpServer) -> None:
    """基线结论都看不到，就没有"对比"可言 —— 不能回一份空差异让人以为没影响。"""
    payload = _call(
        server,
        "counterfactual_run",
        {"case_id": "CF1", "event_id": EVENT, "as_of": EVENT_AS_OF, "caller_labels": ["ORG-ROAD"]},
    )
    assert payload["status"] == "error"
    assert payload["error"]["code"] == "PERMISSION_DENIED"
