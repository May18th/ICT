# 呈现层闭环执行记录（2026-10-31）

对应 `docs/competition/technical-breadth-and-presentation-boundary.md` §5 的最小闭环三项。
判定口径未变：**任何一次 3D 投入，必须能回答"它让哪一条推演结论在画面上被验证或被证伪"。**

## 0 结论

- 数据链路已经闭环：RenderJob 落盘 → 高亮 id 全部解析到真源 twin → UE 侧按 `twin:` tag 换高亮材质
  → ack 写回 → `scene_render`/`delivery_state` 返回 `acked` → `tool_log.jsonl` 有记录。
- 两条负例在数据层成立：证据不足不落作业、不高亮；账本被篡改时 verify 定位断点。
- **未完成**：UE 内侧"结论不可信"横幅截图未落成可交付证据（原因见 §4），需要按编辑器内脚本路径补一次。

## 1 实体重投影（改了什么）

| 位置 | 改动 |
|---|---|
| `src/decision_poc/scene_layout.py` | 四类布局对象（road / pipeline / facility / water）新增 `twin_id` 字段，按真源规则生成：`TW-road-*`、`TW-<kind>-<pipeline_id>`、`pump_station→TW-pump_station-*`、`intake→TW-drainage-*`、水面→`TW-incident-<工单>` |
| `src/decision_poc/replay_export.py` | 删除 `f"TW-water-{event_id}"` 假 id；新增 `resolve_flood_twin()`：优先 incident twin，其次真源 water twin，找不到返回空串（禁止再拼 id） |
| `visualization/unreal/Content/Python/build_urban_level.py` | 四个 tag 生成点改为消费布局里的 `twin_id`（保留旧规则作 fallback），水面 Actor 挂 `twin:TW-incident-*` |
| `tests/test_presentation_contract.py`（新） | 5 条不变量：积水 twin ∈ 真源、布局 twin_id ⊆ 真源、水面不再造假 id、回放第一跳 = 积水 twin、RenderJob 全部 highlights ⊆ 真源且物理对象 ⊆ 布局 |

重投影结果（实测）：

- `scene_layout.json` 重新生成：7 路 / 8 管线 / 6 设施 / 120 建筑 / 2 水面，**全部 23 个 twin_id 均在 `scene.json` 内**；
- RenderJob（`snap-3d7714d7230412bf`，14 跳）：**19/19 高亮 id 都在 `scene.json`**（改前 18/19，缺 `TW-water-WO-20261029-007`）；
- 第一跳 `observed` 高亮 = `TW-incident-WO-20261029-007`（真源 incident twin）。

UE 关卡（`/Game/UrbanTwin/Maps/UrbanTwinDemo`）按新规则回填了 5 个 Actor 的 tag 并保存：
水面 patch → `twin:TW-incident-WO-20261029-007`；3 个雨水口 `TW-intake-DF-IN-*` → `TW-drainage-DF-IN-*`。

## 2 高亮驱动（验收证据）

| 验收项 | 结果 | 证据 |
|---|---|---|
| 一条命令落盘 RenderJob | ✅ | `UrbanToolkit(log_path=artifacts/visual/tool_log.jsonl).call("scene_render", …)` → `jobs/snap-3d7714d7230412bf.json`（14 跳 / 19 高亮） |
| UE 侧演出并写 ack | ✅（数据层） | UE 内 `M_Highlight` 材质按第一跳 id 换到水面 Actor（画面已确认：水面变青色；见 §4 关于截图文件的说明）；`POST /api/scene/snap-3d7714d7230412bf/ack` → `{"status":"acked"}`，`acks/snap-3d7714d7230412bf.json` 落盘 |
| `scene_render`/delivery 返回 acked | ✅ | `visualize.delivery_state("snap-3d7714d7230412bf") == "acked"` |
| `tool_log.jsonl` 有记录 | ✅ | 3 条 `origin=mcp` 记录，含 `scene_render`（其中 1 条为本轮闭环调用），`snapshot_hash=9b60debf…` |
| HTTP 契约（web_demo 18787） | ✅ | `GET /api/scene/latest` 返回 RenderJob；`POST …/ack` 返回 acked；`/api/tool-log` 返回 mcp 记录 |

## 3 两条负例（数据层）

| 负例 | 结果 | 证据 |
|---|---|---|
| 证据不足（I01 未知事件 `WO-2099-000`） | 不推演、不落作业、不高亮 | `event_deduce` → `status=error, code=NOT_FOUND`；`scene_render` → `status=error, code=EVIDENCE_INSUFFICIENT`；`jobs/` 目录 hash 前后一致、无新作业 |
| 账本被篡改 | verify 定位断点，可切"结论不可信" | 篡改前 `verify ok, checked=1`；篡改后 `ok=false, first_broken=PV-0001-13ca217b, reason=HASH_MISMATCH` |

## 4 UE 内侧画面证据（2026-10-31 补拍完成）

在编辑器里用"同一 MCP 会话"完成 load_level → 找 `twin:` Actor → 换 `M_Highlight` → 截图，
避开了此前跨会话 world 绑定导致拍到 `UrbanTwinLevel` 的问题。四张证据已入仓
`docs/reports/captures/`（UE 视口 1534×844）：

| 文件 | 内容 |
| --- | --- |
| `S31-ue-contract-level-baseline.png` | 基线：`Maps/UrbanTwinDemo` 契约关卡，水面 patch 未高亮 |
| `S31-ue-highlight-step0.png` | 正例：第一跳 `observed` 高亮 → 水面 patch 换 `M_Highlight` 变青色（对应 `TW-incident-WO-20261029-007`） |
| `S31-ue-negative-insufficient.png` | 负例 1：I01 证据不足 → **不高亮**（水面恢复常态），横幅 `EVIDENCE INSUFFICIENT (I01) - no highlight` |
| `S31-ue-negative-tampered.png` | 负例 2：账本被篡改 → 画面切 `CONCLUSION UNTRUSTED - ledger break PV-0001` |

关卡 `UrbanTwinDemo` 已保存为干净状态（横幅 Actor 已删除、水面材质 override 已重置）。

说明：横幅用 ASCII 是刻意的——TextRender 默认字体缺中文字形，中文会显示成 `?`；
正式演示时如需中文，给 TextRender 换一个含中文字形的 Font 资产即可（不影响契约）。

## 5 回归

- `python -m pytest -q` → **521 passed**（新增 5 条呈现层契约不变量；测试数已冻结在
  `samples/eval/evidence_expectations.json` 的 `test_baseline.collected=521`）；
- `python -m decision_poc.evidence_check` → **106/106 项一致**，期望指纹 `73b104597a9c2318`
  （`checks.test_suite.docs_declared=[521]` 会扫描 README/竞赛文档，写旧数字即失败）。

## 6 下一步（按 0.5–1 人日剩余部分）

1. 编辑器内跑 `urban_twin_state.sync_from_api()`（或设 `UrbanTwinDemo` 为启动地图），重拍：
   正例高亮、I01 不高亮、账本篡改横幅（横幅建议英文/ASCII，避免字体缺字）；
2. 把这张"画面与 ack 同屏"的证据挂进 PPT/答辩脚本第 5 步（一条命令自检之后）；
3. 之后所有 3D/视觉工作继续冻结，时间按 `competition-gap-audit.md` 的 P0/P1/P2 回流。
