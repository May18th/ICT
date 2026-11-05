"""MCP server exposing the twin as six tools (P5).

Transport note: JSON-RPC 2.0 over stdio, newline-delimited, implemented directly
instead of pulling in an SDK. The dataset is 15 assets; a protocol library would
add install surface to a project whose acceptance includes "clean environment
reproduces from README".

`McpServer.handle()` is transport-agnostic and returns a dict (or None for
notifications). `serve_stdio()` is a thin loop around it, so tests exercise the
protocol without spawning processes, and an HTTP/SSE adapter for Nexent later is
a new loop over the same `handle()`, not a rewrite.

Tool contract: every tool returns the existing `ToolResult` envelope
(`status` / `data` / `error` / `stats` / `context`). No new envelope type -- that
is what lets Skills compose and swap tools.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from pydantic import ValidationError

from .contracts import ToolContext, ToolError, ToolResult, ToolStats
from .governance import ErrorCode, blocked_by_visibility, is_retryable
from .provenance import ProvenanceLedger, build_ledger, split_ref
from .semantic import (
    ObjectKind,
    Predicate,
    SemanticQuery,
    TwinSemanticStore,
    build_urban_semantic_store,
)

SERVER_NAME = "urban-twin-mcp"
SERVER_VERSION = "0.6.0"
PROTOCOL_VERSION = "2025-06-18"

# 工具分组（allowlist）。目的：智能体在推演循环里看不见渲染工具，就不会把推理预算
# 花在"要不要换个镜头"上。`presentation` 只在决策定稿后启用，`bootstrap` 只在初始化启用。
TOOL_GROUPS: dict[str, tuple[str, ...]] = {
    "decision": (
        "semantic_query",
        "twin_neighbors",
        "evidence_locate",
        "claim_trace",
        "provenance_verify",
        "event_deduce",
        "counterfactual_run",
        "conflict_check",
        "allocation_plan",
    ),
    "presentation": ("scene_render", "scene_status", "scene_compare"),
    "bootstrap": ("asset_lookup",),
    # 多智能体协同：总控一次调用拿到 6 个角色的动作卡、联合发布对与裁决后的处置顺序
    "collaboration": ("collab_run",),
}

#: 部门 Agent 的工具白名单。与 `TOOL_GROUPS` 分开维护：前者是**互斥分区**
#: （一个工具只属于一个分组，tools.json 与分区一致性测试依赖这一点），
#: 后者是**运行期视图**（同一工具可以出现在多个部门视角里），两者语义不同。
ROLE_TOOL_GROUPS: dict[str, tuple[str, ...]] = {
    "role_drain": ("twin_neighbors", "event_deduce", "allocation_plan"),
    "role_road": ("twin_neighbors", "evidence_locate"),
    "role_traffic": ("twin_neighbors", "evidence_locate"),
    "role_gas": ("twin_neighbors", "claim_trace"),
    "role_power": ("twin_neighbors", "allocation_plan"),
}

ALL_TOOL_GROUPS: dict[str, tuple[str, ...]] = {**TOOL_GROUPS, **ROLE_TOOL_GROUPS}


# --- JSON-RPC error codes (spec) ----------------------------------------
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603


def _ok(source: str, data: dict[str, Any], version: str | None = None) -> ToolResult:
    return ToolResult(
        status="ok",
        data=data,
        error=None,
        stats=ToolStats(attempt=1, latency_ms=0),
        context=ToolContext(source=source, version=version),
    )


def _error(source: str, data: dict[str, Any], code: ErrorCode, message: str, version: str | None = None) -> ToolResult:
    return ToolResult(
        status="error",
        data=data,
        error=ToolError(code=code.value, message=message, retryable=is_retryable(code)),
        stats=ToolStats(attempt=1, latency_ms=0),
        context=ToolContext(source=source, version=version),
    )


class LocatorError(ValueError):
    pass


def parse_locator(locator: str) -> tuple[str, dict[str, Any]]:
    """`A-03/drainage_facility/v1#row:4` -> ("row", {"start": 4, "end": 4})."""
    if "#" not in locator:
        raise LocatorError(f"locator missing #fragment: {locator!r}")
    _, fragment = locator.rsplit("#", 1)
    if ":" not in fragment:
        raise LocatorError(f"locator fragment missing kind: {fragment!r}")
    kind, payload = fragment.split(":", 1)
    if kind in {"line", "row"}:
        if "-" in payload:
            start_raw, end_raw = payload.split("-", 1)
        else:
            start_raw = end_raw = payload
        try:
            start, end = int(start_raw), int(end_raw)
        except ValueError as exc:
            raise LocatorError(f"non-numeric {kind}: {payload!r}") from exc
        if start < 1 or end < start:
            raise LocatorError(f"invalid span: {payload!r}")
        return kind, {"start": start, "end": end}
    if kind == "region":
        parts = payload.split(",")
        if len(parts) != 4:
            raise LocatorError(f"region needs x,y,w,h: {payload!r}")
        try:
            x, y, w, h = (int(part) for part in parts)
        except ValueError as exc:
            raise LocatorError(f"non-numeric region: {payload!r}") from exc
        return kind, {"bbox": [x, y, w, h]}
    raise LocatorError(f"unsupported locator kind: {kind!r}")


class UrbanToolkit:
    """Holds the assembled state once; tools are read-only lookups."""

    def __init__(
        self,
        dataset_root: str | None = None,
        *,
        knowledge_version: str = "kb-v1.0.0",
        ledger_dir: str | None = None,
        store: TwinSemanticStore | None = None,
        ledger: ProvenanceLedger | None = None,
        log_path: str | None = None,
        rules_path: str | None = None,
        visual_root: str | None = None,
        groups: Sequence[str] | None = None,
    ) -> None:
        self.logs: list[dict[str, Any]] = []
        self.log_path = Path(log_path) if log_path else None
        # 渲染作业根目录：None 表示走 visualize 的默认值（可用环境变量覆盖）
        self.visual_root = Path(visual_root) if visual_root else None
        # 工具分组：None = 全量暴露，保持向后兼容
        self.groups = tuple(groups) if groups else None
        self._deduction: Any = None
        self._skills: Any = None
        self.rules_path = Path(rules_path) if rules_path else _default_rules_path()
        from .urban_dataset import DEFAULT_DATASET_ROOT, load_urban_assets

        self.dataset_root = Path(dataset_root) if dataset_root else DEFAULT_DATASET_ROOT
        self.knowledge_version = knowledge_version
        rows = load_urban_assets(self.dataset_root)
        self._assets = {asset.asset_id: asset for asset, _ in rows}
        self._governance = {asset.asset_id: record for asset, record in rows}
        self.store = store or build_urban_semantic_store(
            str(self.dataset_root), knowledge_version=knowledge_version
        )
        if ledger is not None:
            self.ledger = ledger
        elif ledger_dir:
            self.ledger = ProvenanceLedger.load(
                ledger_dir,
                asset_fingerprints={asset_id: asset.checksum_sha256 for asset_id, asset in self._assets.items()},
                knowledge_version=knowledge_version,
            )
        else:
            self.ledger = build_ledger(str(self.dataset_root), knowledge_version=knowledge_version)

    # --- schema ----------------------------------------------------------
    @property
    def version(self) -> str:
        return self.knowledge_version

    def tools(self, groups: Sequence[str] | None = None) -> list[dict[str, Any]]:
        tools = [
            {
                "name": "semantic_query",
                "description": "按时间点查询孪生对象与语义关系，返回三元组、对象与 snapshot_hash。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "query_id": {"type": "string"},
                        "as_of": {"type": "string", "description": "ISO 8601 带时区"},
                        "seed_ids": {"type": "array", "items": {"type": "string"}},
                        "predicates": {
                            "type": "array",
                            "items": {"type": "string", "enum": [item.value for item in Predicate]},
                        },
                        "kind_filter": {
                            "type": "array",
                            "items": {"type": "string", "enum": [item.value for item in ObjectKind]},
                        },
                        "max_hops": {"type": "integer", "minimum": 1, "maximum": 6},
                        "min_confidence": {"type": "number", "minimum": 0, "maximum": 1},
                        "caller_labels": {"type": "array", "items": {"type": "string"}},
                        "include_candidates": {"type": "boolean"},
                        "block_expansion_through": {
                            "type": "array",
                            "items": {"type": "string", "enum": [item.value for item in ObjectKind]},
                        },
                    },
                    "required": ["query_id", "as_of", "seed_ids"],
                },
            },
            {
                "name": "twin_neighbors",
                "description": "从一个孪生对象出发取邻接关系，可选屏蔽某些类型作为中转（如片区）。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "twin_id": {"type": "string"},
                        "as_of": {"type": "string"},
                        "max_hops": {"type": "integer", "minimum": 1, "maximum": 6},
                        "caller_labels": {"type": "array", "items": {"type": "string"}},
                        "block_expansion_through": {
                            "type": "array",
                            "items": {"type": "string", "enum": [item.value for item in ObjectKind]},
                        },
                    },
                    "required": ["twin_id", "as_of"],
                },
            },
            {
                "name": "evidence_locate",
                "description": "按 evidence_ref（asset_id#locator）取回原始片段：表格行、文段行号或图片标注区域。受限资产需调用方标签匹配。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "evidence_ref": {"type": "string"},
                        "caller_labels": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["evidence_ref"],
                },
            },
            {
                "name": "claim_trace",
                "description": "追溯一条 claim：来源资产、定位、指纹、规则出处、所属记录与决策。",
                "inputSchema": {
                    "type": "object",
                    "properties": {"claim_id": {"type": "string"}},
                    "required": ["claim_id"],
                },
            },
            {
                "name": "provenance_verify",
                "description": "校验溯源哈希链；给 decision_id 时返回该结论的最小重放证据集。",
                "inputSchema": {
                    "type": "object",
                    "properties": {"decision_id": {"type": "string"}},
                },
            },
            {
                "name": "event_deduce",
                "description": "跨部门六跳推演：现象→受影响对象→责任部门→职责冲突→资源需求→处置动作。依据受限时返回 PERMISSION_DENIED。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string", "description": "工单编号，如 WO-20261029-007"},
                        "as_of": {"type": "string", "description": "ISO 8601 带时区"},
                        "caller_labels": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["event_id", "as_of", "caller_labels"],
                },
            },
            {
                "name": "counterfactual_run",
                "description": "反事实分支：改一个输入（库存/工期/处置顺序）后重放同一套规则与语义层，返回与基线的差异；算不出来的部分显式列出。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "case_id": {"type": "string", "enum": ["CF1", "CF2", "CF3"]},
                        "event_id": {"type": "string"},
                        "as_of": {"type": "string", "description": "ISO 8601 带时区"},
                        "caller_labels": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["case_id", "event_id", "as_of", "caller_labels"],
                },
            },
            {
                "name": "conflict_check",
                "description": "只取冲突视图：职责重叠、职责空白、资源争用，附建议主责。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string"},
                        "as_of": {"type": "string"},
                        "caller_labels": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["event_id", "as_of", "caller_labels"],
                },
            },
            {
                "name": "allocation_plan",
                "description": "稀缺抽排资源分配：取推演的片区决策输入，按可版本化策略给出各片区分配、跨区调拨与需上报增援的缺口。策略参数可选，便于编排层使用进化后的版本。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string", "description": "工单编号，如 WO-20261029-007"},
                        "as_of": {"type": "string", "description": "ISO 8601 带时区"},
                        "caller_labels": {"type": "array", "items": {"type": "string"}},
                        "policy": {
                            "type": "object",
                            "description": "可选策略参数（P9 进化后的版本）；缺省为 v1 各片区用本区库存",
                            "properties": {
                                "policy_version": {"type": "string"},
                                "weight_closure": {"type": "number", "minimum": 0},
                                "weight_hazard": {"type": "number", "minimum": 0},
                                "weight_depth": {"type": "number", "minimum": 0},
                                "depth_scale_cm": {"type": "number", "exclusiveMinimum": 0},
                                "transfer_penalty": {"type": "number", "minimum": 0},
                                "allow_transfer_from_critical": {"type": "boolean"},
                            },
                        },
                    },
                    "required": ["event_id", "as_of", "caller_labels"],
                },
            },
            {
                "name": "scene_render",
                "description": "把一次决策渲染成三维推演作业（UE 拉取后演）。给 snapshot_id 直接渲已有快照；给 event_id/as_of/policy 则先跑决策再渲。只接受快照与策略，不接受位姿/材质/镜头。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "snapshot_id": {
                            "type": "string",
                            "description": "已导出的快照 id；给了它就不再重算决策",
                        },
                        "event_id": {"type": "string", "description": "工单编号，缺省 WO-20261029-007"},
                        "as_of": {"type": "string", "description": "ISO 8601 带时区"},
                        "caller_labels": {"type": "array", "items": {"type": "string"}},
                        "policy": {
                            "type": "object",
                            "description": "可选策略参数（P9 进化后的版本）；换策略即换 snapshot_id",
                            "properties": {
                                "policy_version": {"type": "string"},
                                "weight_closure": {"type": "number", "minimum": 0},
                                "weight_hazard": {"type": "number", "minimum": 0},
                                "weight_depth": {"type": "number", "minimum": 0},
                                "depth_scale_cm": {"type": "number", "exclusiveMinimum": 0},
                                "transfer_penalty": {"type": "number", "minimum": 0},
                                "allow_transfer_from_critical": {"type": "boolean"},
                            },
                        },
                    },
                },
            },
            {
                "name": "scene_status",
                "description": "查一次渲染作业的出片状态：pending（还没渲）还是 acked（已有成片）。"
                "acked 时返回成片路径、大小与 sha256，用于核对'平台拿到的片子就是那一份结论'。"
                "只读，不触发渲染。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "snapshot_id": {
                            "type": "string",
                            "description": "快照 id；缺省取最近一次作业",
                        },
                    },
                },
            },
            {
                "name": "scene_compare",
                "description": "对比两份推演快照（基线 vs 进化后策略）：策略改了哪些参数、各片区分配怎么变、调拨与未满足怎么变，给一句可直接讲给评委的结论。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "baseline_id": {"type": "string"},
                        "candidate_id": {"type": "string"},
                    },
                    "required": ["baseline_id", "candidate_id"],
                },
            },
            {
                "name": "asset_lookup",
                "description": "核对 Blender 侧是否已有该场景的可导入资产（glb/blend/对象计数）。只核对不生成：资产生成是离线动作，缺失时返回可执行的离线命令。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "scenario": {
                            "type": "string",
                            "enum": ["normal", "flood", "closure", "construction"],
                        },
                    },
                    "required": ["scenario"],
                },
            },
            {
                "name": "collab_run",
                "description": "多智能体协同：1 总控 + 5 专业部门在协同板上出动作卡，"
                "按规则完成联合发布判定与处置顺序编排，返回卡片、冲突、交接与内核指纹。"
                "结论仍由六跳推演产出，Agent 只出动作不出断言。",
                "inputSchema": {
                    "type": "object",
                    "properties": {
                        "event_id": {"type": "string", "description": "工单编号，如 WO-20261029-007"},
                        "as_of": {"type": "string", "description": "ISO 8601 带时区"},
                        "caller_labels": {"type": "array", "items": {"type": "string"}},
                    },
                    "required": ["event_id", "as_of", "caller_labels"],
                },
            },
        ]

        selected = self._selected_groups(groups)
        if selected is None:
            return tools
        allowed = {name for group in selected for name in ALL_TOOL_GROUPS[group]}
        return [tool for tool in tools if tool["name"] in allowed]

    def _selected_groups(
        self, groups: Sequence[str] | None = None
    ) -> tuple[str, ...] | None:
        selected = tuple(groups) if groups else self.groups
        if not selected:
            return None
        unknown = [group for group in selected if group not in ALL_TOOL_GROUPS]
        if unknown:
            raise KeyError(f"未知工具分组: {unknown}，可选 {sorted(ALL_TOOL_GROUPS)}")
        return selected

    # --- dispatch --------------------------------------------------------
    def call(
        self, name: str, arguments: Mapping[str, Any] | None, *, trace_id: str | None = None
    ) -> ToolResult:
        handlers: dict[str, Callable[[Mapping[str, Any]], ToolResult]] = {
            "semantic_query": self._semantic_query,
            "twin_neighbors": self._twin_neighbors,
            "evidence_locate": self._evidence_locate,
            "claim_trace": self._claim_trace,
            "provenance_verify": self._provenance_verify,
            "event_deduce": self._event_deduce,
            "conflict_check": self._conflict_check,
            "counterfactual_run": self._counterfactual_run,
            "allocation_plan": self._allocation_plan,
            "scene_render": self._scene_render,
            "scene_status": self._scene_status,
            "scene_compare": self._scene_compare,
            "asset_lookup": self._asset_lookup,
            "collab_run": self._collab_run,
        }
        handler = handlers.get(name)
        if handler is None:
            raise KeyError(name)
        started = time.perf_counter()
        try:
            result = handler(arguments or {})
        except ValidationError as exc:
            result = _error(
                name,
                {"invalid": json.loads(exc.json())},
                ErrorCode.VALIDATION_FAILED,
                f"参数校验失败: {exc.error_count()} 处",
                self.version,
            )
        except LocatorError as exc:
            result = _error(name, {}, ErrorCode.VALIDATION_FAILED, str(exc), self.version)
        elapsed = int((time.perf_counter() - started) * 1000)
        result = result.model_copy(
            update={
                "stats": ToolStats(attempt=1, latency_ms=elapsed),
                "context": result.context.model_copy(update={"trace_id": trace_id}),
            }
        )
        self._log(name, trace_id, result)
        return result

    def _log(self, name: str, trace_id: str | None, result: ToolResult) -> None:
        entry = {
            "tool": name,
            # 带时间戳：日志是容器内累积文件、没有会话维度，只有时间才能把某次调用
            # 归属到某一轮会话（否则"这 3 条 event_deduce 是哪一轮的"说不清）。
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            # 演示现场要能区分"智能体调的"和"人手动点的"，所以带来源
            "origin": "mcp",
            "trace_id": trace_id,
            "status": result.status,
            "error_code": result.error.code if result.error else None,
            "latency_ms": result.stats.latency_ms,
            "knowledge_version": self.version,
            "snapshot_hash": self.store.snapshot_hash(),
        }
        self.logs.append(entry)
        if self.log_path is not None:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")

    # --- tools -----------------------------------------------------------
    def _semantic_query(self, args: Mapping[str, Any]) -> ToolResult:
        query = SemanticQuery(
            query_id=str(args["query_id"]),
            as_of=args["as_of"],
            seed_ids=[str(item) for item in args.get("seed_ids", [])],
            predicates=[Predicate(item) for item in args.get("predicates", [])],
            kind_filter=[ObjectKind(item) for item in args.get("kind_filter", [])],
            max_hops=int(args.get("max_hops", 2)),
            min_confidence=float(args.get("min_confidence", 0.6)),
            caller_labels=[str(item) for item in args.get("caller_labels", [])],
            include_candidates=bool(args.get("include_candidates", False)),
            block_expansion_through=[
                ObjectKind(item) for item in args.get("block_expansion_through", [])
            ],
        )
        return self.store.query(query)

    def _twin_neighbors(self, args: Mapping[str, Any]) -> ToolResult:
        twin_id = str(args["twin_id"])
        if twin_id not in {item.twin_id for item in self.store.objects()}:
            return _error(
                "twin_neighbors", {"twin_id": twin_id}, ErrorCode.NOT_FOUND, f"未知孪生对象: {twin_id}", self.version
            )
        query = SemanticQuery(
            query_id=f"neighbors-{twin_id}",
            as_of=args["as_of"],
            seed_ids=[twin_id],
            max_hops=int(args.get("max_hops", 2)),
            caller_labels=[str(item) for item in args.get("caller_labels", [])],
            block_expansion_through=[
                ObjectKind(item) for item in args.get("block_expansion_through", [])
            ],
        )
        result = self.store.query(query)
        result.data["twin_id"] = twin_id
        return result

    def _evidence_locate(self, args: Mapping[str, Any]) -> ToolResult:
        ref = str(args["evidence_ref"])
        try:
            asset_id, locator = split_ref(ref)
        except ValueError as exc:
            return _error("evidence_locate", {"evidence_ref": ref}, ErrorCode.VALIDATION_FAILED, str(exc), self.version)

        asset = self._assets.get(asset_id)
        if asset is None:
            return _error(
                "evidence_locate", {"evidence_ref": ref}, ErrorCode.NOT_FOUND, f"未知资产: {asset_id}", self.version
            )
        # 权利边界：受限资产（如部门规程）的正文不能因为"知道 ref"就被取走
        record = self._governance.get(asset_id)
        visibility = set(getattr(record, "visibility", ()) or ())
        caller_labels = {str(item) for item in args.get("caller_labels", [])}
        if visibility and not (visibility & caller_labels):
            return _error(
                "evidence_locate",
                {"evidence_ref": ref, "required_labels": sorted(visibility)},
                ErrorCode.PERMISSION_DENIED,
                "该资产对当前调用方不可见",
                self.version,
            )
        if not locator.startswith(asset.locator):
            return _error(
                "evidence_locate",
                {"evidence_ref": ref, "expected_prefix": asset.locator},
                ErrorCode.VALIDATION_FAILED,
                "locator 与资产不匹配，可能是伪引用",
                self.version,
            )

        kind, parsed = parse_locator(locator)
        data: dict[str, Any] = {
            "evidence_ref": ref,
            "asset": {
                "asset_id": asset.asset_id,
                "file_name": asset.file_name,
                "doc_type": asset.doc_type,
                "version": asset.version,
                "checksum_sha256": asset.checksum_sha256,
                "locator": asset.locator,
            },
            "locator_kind": kind,
            "knowledge_version": self.version,
        }

        if kind == "region":
            return self._locate_region(asset.file_name, parsed["bbox"], data)

        path = self.dataset_root / asset.file_name
        if not path.is_file():
            return _error(
                "evidence_locate", data, ErrorCode.NOT_FOUND, f"资产文件缺失: {asset.file_name}", self.version
            )
        lines = path.read_text(encoding="utf-8").splitlines()
        start, end = parsed["start"], parsed["end"]
        if end > len(lines):
            return _error(
                "evidence_locate",
                data | {"line_count": len(lines)},
                ErrorCode.VALIDATION_FAILED,
                f"定位越界: {start}-{end} 超出 {len(lines)} 行",
                self.version,
            )
        data["span"] = {"start": start, "end": end, "line_count": len(lines)}
        data["excerpt"] = "\n".join(lines[start - 1 : end])
        if kind == "row" and asset.file_name.endswith(".csv"):
            data["row"] = _csv_row(lines, start)
        return _ok("evidence_locate", data, self.version)

    def _locate_region(self, file_name: str, bbox: Sequence[int], data: dict[str, Any]) -> ToolResult:
        path = self.dataset_root / "annotations.json"
        if not path.is_file():
            return _error("evidence_locate", data, ErrorCode.NOT_FOUND, "缺少 annotations.json", self.version)
        payload = json.loads(path.read_text(encoding="utf-8"))
        for item in payload.get("annotations", {}).get(file_name, []):
            if list(item.get("bbox", [])) == list(bbox):
                data["region"] = {
                    "label": item.get("label"),
                    "bbox": item.get("bbox"),
                    "confidence": item.get("confidence"),
                    "note": item.get("note", ""),
                }
                data["excerpt"] = f"{item.get('label')} @ {bbox}"
                return _ok("evidence_locate", data, self.version)
        return _error(
            "evidence_locate",
            data | {"bbox": list(bbox)},
            ErrorCode.NOT_FOUND,
            "标注里没有该区域，可能是伪引用",
            self.version,
        )

    def _claim_trace(self, args: Mapping[str, Any]) -> ToolResult:
        claim_id = str(args["claim_id"])
        try:
            trace = self.ledger.trace_claim(claim_id)
        except KeyError:
            return _error(
                "claim_trace", {"claim_id": claim_id}, ErrorCode.NOT_FOUND, f"未知 claim: {claim_id}", self.version
            )
        return _ok("claim_trace", trace.model_dump(mode="json"), self.version)

    def _provenance_verify(self, args: Mapping[str, Any]) -> ToolResult:
        decision_id = args.get("decision_id")
        report = self.ledger.verify()
        data: dict[str, Any] = {
            "verify": report.model_dump(mode="json"),
            "record_count": len(self.ledger.records()),
            "knowledge_version": self.version,
        }
        if decision_id:
            try:
                data["replay"] = self.ledger.replay(str(decision_id)).model_dump(mode="json")
            except KeyError:
                return _error(
                    "provenance_verify",
                    data,
                    ErrorCode.NOT_FOUND,
                    f"未知 decision_id: {decision_id}",
                    self.version,
                )
        if not report.ok:
            data["detail"] = "哈希链已损坏，历史结论不可直接采信"
            return ToolResult(
                status="partial",
                data=data,
                error=None,
                stats=ToolStats(attempt=1, latency_ms=0),
                context=ToolContext(source="provenance_verify", version=self.version),
            )
        return _ok("provenance_verify", data, self.version)

    def _deduction_engine(self):
        """首次调用时装配；规则来源校验失败会在这里暴露，而不是给出无依据的推演。"""
        if self._deduction is None:
            from .deduction import DeductionEngine

            self._deduction = DeductionEngine.load(
                self.dataset_root, rules_path=self.rules_path
            )
        return self._deduction

    def _visibility_blocked(self, refs: Sequence[str], caller_labels: Sequence[str]) -> list[str]:
        """推演用到的依据里，哪些对调用方不可见。

        不能"删掉再给结论"——那会让结论失去溯源；也不能照给——那是越权。只能拒。
        """
        return blocked_by_visibility(
            refs,
            caller_labels,
            {asset_id: record.visibility for asset_id, record in self._governance.items()},
        )

    def _event_deduce(self, args: Mapping[str, Any]) -> ToolResult:
        caller_labels = [str(item) for item in args.get("caller_labels", [])]
        event_id = str(args["event_id"])
        try:
            outcome = self._deduction_engine().deduce(
                event_id, as_of=args["as_of"], caller_labels=caller_labels
            )
        except KeyError:
            return _error(
                "event_deduce", {"event_id": event_id}, ErrorCode.NOT_FOUND, f"未知事件: {event_id}", self.version
            )
        except Exception as exc:  # 规则来源校验失败等
            return _error(
                "event_deduce",
                {"event_id": event_id},
                ErrorCode.EVIDENCE_INSUFFICIENT,
                f"推演依据不可用: {exc}",
                self.version,
            )

        cited = sorted({ref for step in outcome.steps for ref in step.evidence_refs})
        blocked = self._visibility_blocked(cited, caller_labels)
        if blocked:
            return _error(
                "event_deduce",
                {"event_id": event_id, "blocked_assets": blocked},
                ErrorCode.PERMISSION_DENIED,
                "推演引用了对当前调用方不可见的依据，不能只给结论不给依据",
                self.version,
            )
        return _ok("event_deduce", outcome.model_dump(mode="json"), self.version)

    def _collab_run(self, args: Mapping[str, Any]) -> ToolResult:
        caller_labels = [str(item) for item in args.get("caller_labels", [])]
        event_id = str(args["event_id"])
        try:
            outcome = self._deduction_engine().deduce(
                event_id, as_of=args["as_of"], caller_labels=caller_labels
            )
        except KeyError:
            return _error(
                "collab_run", {"event_id": event_id}, ErrorCode.NOT_FOUND, f"未知事件: {event_id}", self.version
            )
        except Exception as exc:
            return _error(
                "collab_run",
                {"event_id": event_id},
                ErrorCode.EVIDENCE_INSUFFICIENT,
                f"推演依据不可用: {exc}",
                self.version,
            )

        from .collab import orchestrate

        collab = orchestrate(outcome)
        return _ok("collab_run", collab.model_dump(mode="json"), self.version)

    def _conflict_check(self, args: Mapping[str, Any]) -> ToolResult:
        caller_labels = [str(item) for item in args.get("caller_labels", [])]
        event_id = str(args["event_id"])
        try:
            outcome = self._deduction_engine().deduce(
                event_id, as_of=args["as_of"], caller_labels=caller_labels
            )
        except KeyError:
            return _error(
                "conflict_check", {"event_id": event_id}, ErrorCode.NOT_FOUND, f"未知事件: {event_id}", self.version
            )
        except Exception as exc:
            return _error(
                "conflict_check",
                {"event_id": event_id},
                ErrorCode.EVIDENCE_INSUFFICIENT,
                f"推演依据不可用: {exc}",
                self.version,
            )

        suggested = {
            conflict.subject: ("ORG-EMERG" if conflict.kind.value != "gap" else "待明确")
            for conflict in outcome.conflicts
        }
        data = {
            "event_id": event_id,
            "as_of": outcome.root.as_of.isoformat(),
            "conflicts": [conflict.model_dump(mode="json") for conflict in outcome.conflicts],
            "resource_gaps": [gap.model_dump(mode="json") for gap in outcome.resource_gaps],
            "suggested_owner": suggested,
            "kinds": sorted({conflict.kind.value for conflict in outcome.conflicts}),
            "snapshot_hash": outcome.snapshot_hash,
        }
        return _ok("conflict_check", data, self.version)

    def _skill_runtime(self):
        """Skill 层与本服务共用同一份数据集与规则；建一次即可。"""
        if self._skills is None:
            from .skills import SkillRuntime

            self._skills = SkillRuntime(
                self.dataset_root, rules_path=self.rules_path, knowledge_version=self.version
            )
        return self._skills

    def _counterfactual_run(self, args: Mapping[str, Any]) -> ToolResult:
        """反事实重放。

        基线本身算不出来时不给"空差异"——空差异会被读成"没有影响"，这与
        "算不出来"是两件事。所以先看基线的失败码，越权就直接拒。
        """
        from .counterfactual import CASES, CounterfactualUnavailable, run_counterfactual

        case_id = str(args["case_id"]).strip().upper()
        if case_id not in CASES:
            return _error(
                "counterfactual_run",
                {"case_id": case_id, "known": sorted(CASES)},
                ErrorCode.NOT_FOUND,
                f"未知反事实用例: {case_id}",
                self.version,
            )
        labels = [str(item) for item in args.get("caller_labels", [])]
        try:
            result = run_counterfactual(
                case_id,
                event_id=str(args["event_id"]),
                as_of=args["as_of"],
                caller_labels=labels,
                runtime=self._skill_runtime(),
            )
        except CounterfactualUnavailable as exc:
            return _error(
                "counterfactual_run",
                {"case_id": case_id, "event_id": args.get("event_id")},
                exc.code,
                exc.message,
                self.version,
            )
        except KeyError as exc:
            return _error(
                "counterfactual_run",
                {"case_id": case_id, "event_id": args.get("event_id")},
                ErrorCode.NOT_FOUND,
                str(exc),
                self.version,
            )
        except Exception as exc:
            return _error(
                "counterfactual_run",
                {"case_id": case_id},
                ErrorCode.EVIDENCE_INSUFFICIENT,
                f"反事实无法重放: {type(exc).__name__}: {exc}",
                self.version,
            )

        blocked = self._visibility_blocked(result.evidence_refs, labels)
        if blocked:
            return _error(
                "counterfactual_run",
                {"case_id": case_id, "blocked_assets": blocked},
                ErrorCode.PERMISSION_DENIED,
                "反事实引用了对当前调用方不可见的依据",
                self.version,
            )
        return _ok("counterfactual_run", result.model_dump(mode="json"), self.version)

    def _allocation_plan(self, args: Mapping[str, Any]) -> ToolResult:
        """稀缺抽排资源分配（P9 决策层）。

        只做"怎么分"，不改推演：决策输入来自 `DeductionResult.decision_inputs`。
        依据对调用方不可见时直接拒——分配方案会隐含引用片区风险事实，
        不能"删掉依据只给方案"。
        """
        from .allocation import AllocationPolicy, AllocationUnavailable, decide

        caller_labels = [str(item) for item in args.get("caller_labels", [])]
        event_id = str(args["event_id"])
        raw_policy = args.get("policy") or {}
        if not isinstance(raw_policy, Mapping):
            raise ValueError("policy 必须是对象")
        # 默认 v1（各片区用本区库存）；编排层可传入进化后的参数
        policy = AllocationPolicy.model_validate({"policy_version": "v1", **dict(raw_policy)})

        try:
            outcome = self._deduction_engine().deduce(
                event_id, as_of=args["as_of"], caller_labels=caller_labels
            )
        except KeyError:
            return _error(
                "allocation_plan",
                {"event_id": event_id},
                ErrorCode.NOT_FOUND,
                f"未知事件: {event_id}",
                self.version,
            )
        except Exception as exc:  # 规则来源校验失败等
            return _error(
                "allocation_plan",
                {"event_id": event_id},
                ErrorCode.EVIDENCE_INSUFFICIENT,
                f"推演依据不可用: {exc}",
                self.version,
            )

        cited = sorted({ref for step in outcome.steps for ref in step.evidence_refs})
        blocked = self._visibility_blocked(cited, caller_labels)
        if blocked:
            return _error(
                "allocation_plan",
                {"event_id": event_id, "blocked_assets": blocked},
                ErrorCode.PERMISSION_DENIED,
                "分配会隐含引用对当前调用方不可见的依据，不能只给方案不给依据",
                self.version,
            )
        if not outcome.decision_inputs:
            return _error(
                "allocation_plan",
                {"event_id": event_id},
                ErrorCode.EVIDENCE_INSUFFICIENT,
                "推演未产出片区决策输入（资源测算未跑通），拒绝出分配方案",
                self.version,
            )
        try:
            decision = decide(
                outcome.decision_inputs, policy, snapshot_hash=outcome.snapshot_hash
            )
        except AllocationUnavailable as exc:
            return _error(
                "allocation_plan",
                {"event_id": event_id},
                ErrorCode.EVIDENCE_INSUFFICIENT,
                str(exc),
                self.version,
            )
        data = {
            "event_id": event_id,
            "as_of": outcome.root.as_of.isoformat(),
            "policy": policy.model_dump(mode="json"),
            "allocation": decision.model_dump(mode="json"),
            "rules_version": outcome.rules_version,
            "snapshot_hash": outcome.snapshot_hash,
        }
        return _ok("allocation_plan", data, self.version)


    # --- 可视化（P10） -----------------------------------------------------

    def _visual(self):
        """延迟导入：可视化层要读布局文件，不该压在纯语义查询的冷启动路径上。"""
        from . import visualize

        return visualize

    def _scene_render(self, args: Mapping[str, Any]) -> ToolResult:
        """把一次决策变成 UE 能演的渲染作业。

        三条约束落在实现里：只接受快照 id 与策略（不接受位姿/材质/镜头）；作业落盘
        即返回，不等引擎认领；引擎没起只体现为 delivery=pending，不算失败——渲染不得
        进决策主循环的强依赖路径。
        """
        viz = self._visual()
        snapshot_id = args.get("snapshot_id")
        if snapshot_id:
            try:
                snapshot = viz.load_snapshot(str(snapshot_id), self.visual_root)
            except KeyError:
                return _error(
                    "scene_render",
                    {"snapshot_id": str(snapshot_id)},
                    ErrorCode.NOT_FOUND,
                    f"未知快照: {snapshot_id}，请先用 event_id/policy 生成",
                    self.version,
                )
        else:
            raw_policy = args.get("policy")
            if raw_policy is not None and not isinstance(raw_policy, Mapping):
                return _error(
                    "scene_render",
                    {"policy": raw_policy},
                    ErrorCode.VALIDATION_FAILED,
                    "policy 必须是对象",
                    self.version,
                )
            labels = [str(item) for item in args.get("caller_labels", [])] or ["ORG-EMERG"]
            try:
                snapshot = viz.build_visual_snapshot(
                    str(args.get("event_id") or viz.DEFAULT_EVENT_ID),
                    str(args.get("as_of") or viz.DEFAULT_AS_OF),
                    caller_labels=labels,
                    policy=raw_policy,
                    runtime=self._skill_runtime(),
                )
            except Exception as exc:  # 规则来源校验失败等：渲染失败不能把决策层拖挂
                return _error(
                    "scene_render",
                    {"event_id": args.get("event_id")},
                    ErrorCode.EVIDENCE_INSUFFICIENT,
                    f"快照构建失败: {type(exc).__name__}: {exc}",
                    self.version,
                )
            viz.save_snapshot(snapshot, self.visual_root)

        job = viz.build_render_job(snapshot)
        try:
            job_file = viz.save_job(job, self.visual_root)
        except OSError as exc:
            return _error(
                "scene_render",
                {"snapshot_id": snapshot.snapshot_id},
                ErrorCode.UPSTREAM_UNAVAILABLE,
                f"渲染作业落盘失败: {exc}",
                self.version,
            )
        allocation = snapshot.allocation or {}
        data = {
            "snapshot_id": snapshot.snapshot_id,
            "job_id": job.job_id,
            "scene_hash": snapshot.scene_hash,
            "snapshot_hash": snapshot.snapshot_hash,
            "scenario": snapshot.scenario,
            "policy_version": snapshot.policy_version,
            "step_count": len(snapshot.steps),
            "highlight_count": len(snapshot.highlight_union),
            "allocation": {
                "allocated": allocation.get("total_allocated"),
                "demand": allocation.get("total_demand"),
                "unmet": allocation.get("total_unmet"),
                "transfers": len(allocation.get("transfers", [])),
            },
            # 只有 pending / acked 两档：本工具不持有渲染器连接，不谎报"已渲染"
            "delivery": viz.delivery_state(snapshot.snapshot_id, self.visual_root),
            "job_path": str(job_file),
            "fetch_url": f"/api/scene/{snapshot.snapshot_id}",
            "warnings": list(snapshot.warnings),
        }
        return _ok("scene_render", data, self.version)

    def _scene_status(self, args: Mapping[str, Any]) -> ToolResult:
        """只读出片状态。不触发渲染：渲不渲由 `decision_poc.render_worker` 决定。"""
        viz = self._visual()
        raw_id = args.get("snapshot_id")
        if raw_id:
            snapshot_id = str(raw_id)
            if not viz.job_path(snapshot_id, self.visual_root).is_file():
                return _error(
                    "scene_status",
                    {"snapshot_id": snapshot_id},
                    ErrorCode.NOT_FOUND,
                    f"未知渲染作业: {snapshot_id}（先调 scene_render）",
                    self.version,
                )
        else:
            snapshot_id = viz.latest_snapshot_id(self.visual_root)
            if not snapshot_id:
                return _error(
                    "scene_status",
                    {},
                    ErrorCode.NOT_FOUND,
                    "作业目录为空（先调 scene_render）",
                    self.version,
                )

        ack = viz.read_ack(snapshot_id, self.visual_root) or {}
        artifact = dict(ack.get("artifact") or {})
        data = {
            "snapshot_id": snapshot_id,
            "job_id": f"render-{snapshot_id}",
            "delivery": viz.delivery_state(snapshot_id, self.visual_root),
            "engine": ack.get("engine"),
            "acked_at": ack.get("acked_at"),
            "artifact": artifact,
            "fetch_url": f"/api/scene/{snapshot_id}",
            # 给平台侧一句话说清"接下来该干什么"
            "next_action": (
                "已出片，可直接取 artifact.path / fetch_url"
                if artifact
                else "作业已落盘但引擎未认领：跑 decision_poc.render_worker --snapshot-id "
                     f"{snapshot_id} --mode asset"
            ),
        }
        return _ok("scene_status", data, self.version)

    def _scene_compare(self, args: Mapping[str, Any]) -> ToolResult:
        """基线 vs 进化后策略的差异。两份快照必须都已落盘。"""
        viz = self._visual()
        baseline_id = str(args["baseline_id"])
        candidate_id = str(args["candidate_id"])
        try:
            baseline = viz.load_snapshot(baseline_id, self.visual_root)
            candidate = viz.load_snapshot(candidate_id, self.visual_root)
        except KeyError as exc:
            return _error(
                "scene_compare",
                {"baseline_id": baseline_id, "candidate_id": candidate_id},
                ErrorCode.NOT_FOUND,
                str(exc),
                self.version,
            )
        result = viz.compare_snapshots(baseline, candidate)
        return _ok("scene_compare", result.model_dump(mode="json"), self.version)

    def _asset_lookup(self, args: Mapping[str, Any]) -> ToolResult:
        """核对 Blender 资产是否就绪。不启动 Blender：生成是分钟级离线动作。"""
        viz = self._visual()
        scenario = str(args["scenario"])
        try:
            data = viz.asset_lookup(scenario)
        except KeyError as exc:
            return _error(
                "asset_lookup",
                {"scenario": scenario, "known": list(viz.SCENARIOS)},
                ErrorCode.NOT_FOUND,
                str(exc),
                self.version,
            )
        except FileNotFoundError as exc:
            return _error(
                "asset_lookup",
                {"scenario": scenario},
                ErrorCode.UPSTREAM_UNAVAILABLE,
                str(exc),
                self.version,
            )
        if not data["glb_exists"]:
            data["detail"] = "资产未生成，先离线跑 regenerate_command 再重试"
        return _ok("asset_lookup", data, self.version)


def _default_rules_path() -> Path:
    from .deduction import DEFAULT_RULES_PATH

    return DEFAULT_RULES_PATH


def _csv_row(lines: Sequence[str], line_no: int) -> dict[str, str]:
    import csv
    import io

    if line_no == 1 or not lines:
        return {}
    reader = list(csv.reader(io.StringIO("\n".join(lines[:line_no]))))
    if len(reader) < 2:
        return {}
    header = [cell.lstrip("\ufeff") for cell in reader[0]]  # CSV 可能带 BOM
    values = reader[-1]
    return {header[i]: values[i] for i in range(min(len(header), len(values)))}


class McpServer:
    def __init__(self, toolkit: UrbanToolkit) -> None:
        self.toolkit = toolkit
        self.initialized = False

    def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        if not isinstance(message, Mapping):
            return _rpc_error(None, INVALID_REQUEST, "请求必须是 JSON 对象")
        if message.get("jsonrpc") != "2.0":
            return _rpc_error(message.get("id"), INVALID_REQUEST, "jsonrpc 必须是 2.0")

        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        if not isinstance(method, str):
            return _rpc_error(request_id, INVALID_REQUEST, "缺少 method")
        if not isinstance(params, Mapping):
            return _rpc_error(request_id, INVALID_PARAMS, "params 必须是对象")

        if request_id is None:  # notification: no response, ever
            if method == "notifications/initialized":
                self.initialized = True
            return None

        if method == "initialize":
            self.initialized = True
            return _rpc_result(
                request_id,
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {"tools": {"listChanged": False}},
                    "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
                },
            )
        if method == "ping":
            return _rpc_result(request_id, {})
        if method == "tools/list":
            return _rpc_result(request_id, {"tools": self.toolkit.tools()})
        if method == "tools/call":
            return self._call_tool(request_id, params)
        return _rpc_error(request_id, METHOD_NOT_FOUND, f"未知方法: {method}")

    def _call_tool(self, request_id: Any, params: Mapping[str, Any]) -> dict[str, Any]:
        name = params.get("name")
        arguments = params.get("arguments") or {}
        if not isinstance(name, str):
            return _rpc_error(request_id, INVALID_PARAMS, "tools/call 需要 name")
        if not isinstance(arguments, Mapping):
            return _rpc_error(request_id, INVALID_PARAMS, "arguments 必须是对象")
        trace_id = params.get("trace_id") or (params.get("_meta") or {}).get("trace_id")
        try:
            result = self.toolkit.call(name, arguments, trace_id=trace_id)
        except KeyError:
            return _rpc_error(request_id, INVALID_PARAMS, f"未知工具: {name}")
        except ValidationError as exc:
            return _rpc_error(request_id, INVALID_PARAMS, f"参数校验失败: {exc.error_count()} 处")
        return _rpc_result(
            request_id,
            {
                "content": [{"type": "text", "text": json.dumps(result.model_dump(mode="json"), ensure_ascii=False)}],
                "isError": result.status == "error",
            },
        )


def _rpc_result(request_id: Any, result: dict[str, Any]) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _rpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}


def serve_stdio(toolkit: UrbanToolkit, stdin=None, stdout=None) -> int:
    server = McpServer(toolkit)
    source = stdin or sys.stdin
    sink = stdout or sys.stdout
    for line in source:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError as exc:
            sink.write(json.dumps(_rpc_error(None, PARSE_ERROR, str(exc)), ensure_ascii=False) + "\n")
            sink.flush()
            continue
        response = server.handle(message)
        if response is not None:
            sink.write(json.dumps(response, ensure_ascii=False) + "\n")
            sink.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    # Windows defaults to a legacy console code page; MCP stdio is UTF-8 JSON.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="strict")
    import argparse

    from .visualize import tool_log_path

    parser = argparse.ArgumentParser(description="urban twin MCP server (stdio)")
    parser.add_argument("--dataset", default=None, help="dataset directory")
    parser.add_argument("--ledger", default=None, help="persisted provenance directory")
    parser.add_argument("--version", default="kb-v1.0.0", help="knowledge version to serve")
    parser.add_argument(
        "--log",
        default=None,
        help=f"append tool-call log to this jsonl file（缺省 {tool_log_path()}）",
    )
    parser.add_argument("--visual-root", default=None, help="render job / snapshot root")
    parser.add_argument(
        "--groups",
        default=None,
        help=f"只暴露指定工具分组，逗号分隔（可选 {','.join(sorted(TOOL_GROUPS))}）",
    )
    parser.add_argument("--list-tools", action="store_true", help="print tool schemas and exit")
    parser.add_argument(
        "--export-tools",
        default=None,
        help="把工具清单写成 JSON（供 Nexent 等平台注册，避免手抄 schema）",
    )
    args = parser.parse_args(argv)

    toolkit = UrbanToolkit(
        args.dataset,
        knowledge_version=args.version,
        ledger_dir=args.ledger,
        # 缺省就记到可视化层同一份日志：演示时"谁触发了渲染"要和 MCP 调用日志对得上
        log_path=args.log or str(tool_log_path()),
        rules_path=None,
        visual_root=args.visual_root,
        groups=[item for item in (args.groups or "").split(",") if item] or None,
    )
    if args.list_tools:
        print(json.dumps({"tools": toolkit.tools()}, ensure_ascii=False, indent=2))
        return 0
    if args.export_tools:
        payload = {
            "schema_version": "urban-twin-mcp-tools-v1",
            "server": {
                "name": SERVER_NAME,
                "version": SERVER_VERSION,
                "protocolVersion": PROTOCOL_VERSION,
            },
            # 分组一并导出：平台侧可以按阶段只挂一部分工具
            "groups": {name: list(members) for name, members in TOOL_GROUPS.items()},
            "tools": toolkit.tools(),
        }
        target = Path(args.export_tools)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        print(f"已写出 {target}（{len(payload['tools'])} 个工具 / {len(payload['groups'])} 个分组）")
        return 0
    return serve_stdio(toolkit)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

